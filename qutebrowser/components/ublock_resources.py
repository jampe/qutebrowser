# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Functions related to the ublock origin resources for the Brave adblocker."""

import base64
import dataclasses
import functools
import json
import logging
import io
import pathlib
import re
import urllib.error
import urllib.request
from typing import Optional, IO, List, Dict

from qutebrowser.qt.core import QCoreApplication, QObject, QUrl, pyqtSlot

try:
    from qutebrowser.qt.qml import QJSEngine, QJSValue
    _QJS_AVAILABLE = True
except ImportError:
    QJSEngine = None  # type: ignore[assignment,misc]
    QJSValue = None  # type: ignore[assignment,misc]
    _QJS_AVAILABLE = False

from qutebrowser.api import (
    message,
)
from qutebrowser.components.utils import blockutils
from qutebrowser.components.utils.exceptions import DeserializationError

try:
    import adblock
except ImportError:
    adblock = None  # type: ignore[assignment]

logger = logging.getLogger("network")


@dataclasses.dataclass
class Resource:
    """Class for keeping track of resource information for adblock."""

    name: str
    aliases: List[str]
    content_type: str
    content: str


class ScriptletBridge(QObject):
    """Bridge object exposed to QJSEngine to capture scriptlet registrations."""

    def __init__(self, engine: QJSEngine) -> None:
        super().__init__(parent=engine)
        self.resources: list[Resource] = []

    @pyqtSlot(str, 'QJSValue')
    def registerScriptlet(self, name: str, fn: QJSValue) -> None:
        """Called from JS when builtinScriptlets.push() is called.

        fn is a QJSValue holding a JavaScript function.
        fn.toString() returns the function source code.
        """
        source = fn.toString()
        if not source:
            return
        content = base64.b64encode(source.encode()).decode("utf-8")
        self.resources.append(Resource(
            name=name,
            aliases=[],
            content_type="application/javascript",
            content=content,
        ))

    @pyqtSlot(str, 'QJSValue')
    def registerAliases(self, name: str, aliases: QJSValue) -> None:
        """Called from JS to set aliases for a scriptlet."""
        alias_list: list[str] = (
            aliases.toVariant()
            if not aliases.isUndefined() and not aliases.isNull()
            else []
        )
        for r in self.resources:
            if r.name == name:
                r.aliases = alias_list
                break


class EngineInfo:
    """Keeps track of engine object and cache paths.

    Attributes:
        engine: The engine object.
        cache_path: The cache path for the overall engine.
        resources_cache_path: The resources cache path for the engine.
    """

    def __init__(
        self,
        engine: "adblock.Engine",
        cache_path: pathlib.Path,
        resources_cache_path: pathlib.Path,
    ):
        self.engine = engine
        self.cache_path = cache_path
        self.resources_cache_path = resources_cache_path


_REDIRECT_RESOURCES_URL = QUrl(
    "https://github.com/gorhill/uBlock/raw/master/src/js/redirect-resources.js"
)

_SCRIPTLETS_URL = QUrl(
    "https://raw.githubusercontent.com/gorhill/uBlock/master/src/js/resources/scriptlets.js"
)

_SCRIPTLET_MODULES_BASE_URL = (
    "https://raw.githubusercontent.com/gorhill/uBlock/master/src/js/resources/"
)

# Regex to extract filename from: import { ... } from './foo.js'; or import '../foo.js';
# Also handles side-effect imports: import './foo.js'; or import '../foo.js';
_IMPORT_FILE_RE = re.compile(r"""from\s+['"]((?:\.\.?/)[^'"]+)['"]""")
_SIDE_EFFECT_IMPORT_RE = re.compile(r"""^import\s+['"]((?:\.\.?/)[^'"]+)['"]\s*;$""", re.MULTILINE)

_WEB_ACCESSIBLE_RESOURCES_URL_TEMPLATE = (
    "https://github.com/gorhill/uBlock/raw/master/src/web_accessible_resources/{}"
)


def deserialize_resources_from_file(path: pathlib.Path) -> List[Resource]:
    """Deserialize and return resource information from cached resources data file."""
    with open(path, encoding="utf-8") as f:
        try:
            # TODO: In the future, we may want to use a serialization library (such as
            # pyserde) instead of doing this manually like this
            resources = json.load(f)
            return [
                Resource(d["name"], d["aliases"], d["content_type"], d["content"])
                for d in resources
            ]
        except (TypeError, KeyError):
            raise DeserializationError(f"Failed to deserialize {path}")


def _serialize_resources_to_file(resources: List[Resource], path: pathlib.Path) -> None:
    """Serialize resource information to cached resources data file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(resource) for resource in resources], f)


def _parse_resource(
    aliases_map: Dict[str, List[str]],
    url: QUrl,
    fileobj: IO[bytes],
) -> Resource:
    ext_map = {
        ".gif": "image/gif",
        ".html": "text/html",
        ".js": "application/javascript",
        ".json": "application/json",
        ".mp3": "audio/mp3",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".txt": "text/plain",
        ".css": "text/css",
        ".xml": "application/octet-stream",
        "": "application/octet-stream",
    }

    ext = pathlib.Path(url.path()).suffix

    if ext not in ext_map:
        raise ValueError(f"Don't know this extension: {ext}")

    if ext_map[ext] in (
        "application/javascript",
        "text/html",
        "text/plain",
        "text/css",
        "application/json",
    ):
        with io.TextIOWrapper(fileobj, encoding="utf-8") as text_io:
            content = text_io.read().replace("\r", "").encode()
    else:
        content = fileobj.read()

    return Resource(
        pathlib.Path(url.path()).name,
        aliases_map[url.toString()],
        ext_map[ext],
        base64.b64encode(content).decode("utf-8"),
    )


def _parse_import_filenames(content: str) -> set[str]:
    """Extract unique referenced filenames from all import statements."""
    files: set[str] = set()
    for m in _IMPORT_FILE_RE.finditer(content):
        f = m.group(1)
        if f.startswith('./'):
            f = f[2:]
        files.add(f)
    for m in _SIDE_EFFECT_IMPORT_RE.finditer(content):
        f = m.group(1)
        if f.startswith('./'):
            f = f[2:]
        files.add(f)
    return files


def _collect_all_import_content(content: str,
                                _visited: Optional[set[str]] = None
                                ) -> dict[str, str]:
    """Recursively collect content of all imported submodules.

    Walks all import statements in the given content and any imported files,
    downloads each unique file exactly once.
    """
    if _visited is None:
        _visited = set()

    files = _parse_import_filenames(content)
    result: dict[str, str] = {}

    for filename in files:
        if filename in _visited:
            continue
        _visited.add(filename)

        sub_content = _fetch_submodule_text(filename)
        if sub_content is None:
            continue

        result[filename] = sub_content
        # Recursively collect imports from this submodule too
        nested = _collect_all_import_content(sub_content, _visited)
        # Nested results go into our result dict, but don't overwrite top-level
        for k, v in nested.items():
            if k not in result:
                result[k] = v

    return result


def _fetch_submodule_text(filename: str) -> Optional[str]:
    """Download a submodule file from upstream uBlock resources.

    Returns None on failure (logged).
    """
    url = _SCRIPTLET_MODULES_BASE_URL + filename
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read().decode('utf-8')
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.error("braveadblock: Failed to download submodule %s: %s", filename, e)
        return None


def _convert_template_literals(js: str) -> str:  # noqa: C901
    """Convert ES2015 template literals to string concatenation for QJSEngine.

    Uses a state machine that tracks strings, comments, and regex contexts
    so backticks inside regex character classes are not mistaken for
    template literal delimiters.
    """
    # States for the state machine
    NORMAL = 0
    SINGLE_QUOTE = 1
    DOUBLE_QUOTE = 2
    LINE_COMMENT = 3
    BLOCK_COMMENT = 4
    REGEX = 5
    TEMPLATE = 6
    TEMPLATE_EXPR = 7  # Inside ${...} within a template literal

    result: list[str] = []
    state = NORMAL
    template_parts: list[tuple[str, str]] = []  # ('str', text) or ('expr', code)
    template_buf: list[str] = []
    template_expr_depth = 0
    i = 0
    re_brace_depth = 0  # Track regex [...] nesting

    while i < len(js):
        c = js[i]
        nc = js[i + 1] if i + 1 < len(js) else ''

        if state == NORMAL:
            if c == "'":
                state = SINGLE_QUOTE
                result.append(c)
            elif c == '"':
                state = DOUBLE_QUOTE
                result.append(c)
            elif c == '/' and nc == '/':
                state = LINE_COMMENT
                result.append(c)
            elif c == '/' and nc == '*':
                state = BLOCK_COMMENT
                result.append(c)
            elif c == '/':
                # Could be start of regex. Heuristic: if preceded by
                # operator, opening bracket, or start of line -> regex.
                # Simple heuristic: check if prev char suggests regex start.
                prev = result[-1] if result else ''
                # These tokens can precede regex literals
                regex_starters = {
                    '(', '[', '{', '!', '&', '|', ',', ';', '=',
                    '?', ':', '+', '-', '*', '%', '<', '>', '^', '~',
                }
                if prev == '' or prev in regex_starters or prev.isspace():
                    state = REGEX
                    re_brace_depth = 0
                result.append(c)
            elif c == '`':
                # Start of template literal
                state = TEMPLATE
                template_parts = []
                template_buf = []
            else:
                result.append(c)

        elif state == SINGLE_QUOTE:
            result.append(c)
            if c == '\\' and nc:
                result.append(nc)
                i += 1
            elif c == "'":
                state = NORMAL

        elif state == DOUBLE_QUOTE:
            result.append(c)
            if c == '\\' and nc:
                result.append(nc)
                i += 1
            elif c == '"':
                state = NORMAL

        elif state == LINE_COMMENT:
            result.append(c)
            if c == '\n':
                state = NORMAL

        elif state == BLOCK_COMMENT:
            result.append(c)
            if c == '*' and nc == '/':
                result.append(nc)
                i += 1
                state = NORMAL

        elif state == REGEX:
            result.append(c)
            if c == '\\' and nc:
                result.append(nc)
                i += 1
            elif c == '[' and re_brace_depth == 0:
                re_brace_depth = 1
            elif c == ']' and re_brace_depth > 0:
                re_brace_depth = 0
            elif c == '/' and re_brace_depth == 0:
                state = NORMAL

        elif state == TEMPLATE:
            if c == '\\' and nc == '`':
                template_buf.append('`')
                i += 1
            elif c == '\\' and nc == '$':
                template_buf.append('$')
                i += 1
            elif c == '$' and nc == '{':
                # End of string part
                if template_buf:
                    template_parts.append(('str', ''.join(template_buf)))
                    template_buf = []
                state = TEMPLATE_EXPR
                template_expr_depth = 1
                i += 1
            elif c == '`':
                # End of template literal
                if template_buf:
                    template_parts.append(('str', ''.join(template_buf)))
                    template_buf = []
                # Build replacement
                if not template_parts:
                    result.append("''")
                elif len(template_parts) == 1:
                    ptype, pval = template_parts[0]
                    if ptype == 'str':
                        escaped = pval.replace("'", "\\'").replace('\n', '\\n')
                        result.append(f"'{escaped}'")
                    else:
                        result.append(pval)
                else:
                    segments: list[str] = []
                    for ptype, pval in template_parts:
                        if ptype == 'str':
                            escaped = pval.replace("'", "\\'").replace('\n', '\\n')
                            segments.append(f"'{escaped}'")
                        else:
                            segments.append(f'({pval})')
                    result.append('(' + '+'.join(segments) + ')')
                state = NORMAL
            else:
                template_buf.append(c)

        elif state == TEMPLATE_EXPR:
            if c == '{':
                template_expr_depth += 1
                template_buf.append(c)
            elif c == '}':
                template_expr_depth -= 1
                if template_expr_depth == 0:
                    if template_buf:
                        template_parts.append(('expr', ''.join(template_buf)))
                        template_buf = []
                    state = TEMPLATE
                else:
                    template_buf.append(c)
            elif c == "'":
                # Handle single-quoted strings inside ${...}
                template_buf.append(c)
                # Fast-forward to end of string
                i += 1
                while i < len(js):
                    template_buf.append(js[i])
                    if js[i] == '\\' and i + 1 < len(js):
                        template_buf.append(js[i + 1])
                        i += 2
                        continue
                    if js[i] == "'":
                        break
                    i += 1
            elif c == '"':
                # Handle double-quoted strings inside ${...}
                template_buf.append(c)
                i += 1
                while i < len(js):
                    template_buf.append(js[i])
                    if js[i] == '\\' and i + 1 < len(js):
                        template_buf.append(js[i + 1])
                        i += 2
                        continue
                    if js[i] == '"':
                        break
                    i += 1
            else:
                template_buf.append(c)

        i += 1

    return ''.join(result)


def _strip_class_fields(js: str) -> str:
    """Convert ES2022 class instance fields to getter defs for QJSEngine compat.

    QJSEngine does not support `x = y;` class field syntax (ES2022).
    Converts to equivalent `get x() { return y; }` within class bodies.
    Uses brace-depth tracking to determine when inside a class body.
    """
    lines = js.split('\n')
    result: list[str] = []
    brace_depth = 0
    class_depth: Optional[int] = None  # brace depth at which class body started

    for line in lines:
        stripped = line.strip()

        # Detect class declaration: class X {
        if class_depth is None and re.match(r'class\s+\w+\s*\{', stripped):
            class_depth = brace_depth

        # Track brace depth (structural only since comments/strings already processed)
        open_count = line.count('{')
        close_count = line.count('}')

        # Check for field declarations when at class member depth
        if class_depth is not None and brace_depth == class_depth + 1:
            # Matches: field = value; or bare field;
            # But NOT: method() {}, get/set prop() {}, static { ... }
            fld_match = re.match(
                r'^(\s*)(\w+)\s*(?:=\s*(.+))?\s*;\s*$', line
            )
            # Method detection: `(` before any `=` means method signature
            eq_idx = line.find('=')
            paren_idx = line.find('(')
            is_method = paren_idx != -1 and (eq_idx == -1 or paren_idx < eq_idx)
            if fld_match and not (
                stripped.startswith('static ') or
                stripped.startswith('get ') or
                stripped.startswith('set ') or
                stripped.endswith('}') or
                is_method
            ):
                indent, name = fld_match.group(1), fld_match.group(2)
                value = fld_match.group(3)
                if value is not None:
                    result.append(
                        f'{indent}get {name}() {{ return {value}; }}'
                    )
                else:
                    result.append(
                        f'{indent}get {name}() {{ return undefined; }}'
                    )
                brace_depth += open_count - close_count
                continue

        brace_depth += open_count - close_count

        # Detect end of class body (popping back to class-opening depth)
        if class_depth is not None and brace_depth <= class_depth:
            class_depth = None

        result.append(line)

    return '\n'.join(result)


def _strip_module_imports(content: str) -> str:  # noqa: C901
    """Strip all ES module import/export statements from a module file.

    Removes:
    - import './file.js' (side-effect imports)
    - import { x } from './file.js' (named imports, including multi-line)
    - export const/function/class (becomes const/function/class)
    - export { ... } (re-export blocks)
    - export type { ... }
    - import { ... } from '...' with multi-line braces

    Also strips async/await and fixes optional catch binding for QJSEngine.
    """
    lines = content.split('\n')
    filtered: list[str] = []
    in_import_block = False
    in_export_block = False

    for line in lines:
        stripped = line.strip()

        # Handle multi-line import blocks
        if in_import_block:
            if 'from ' in stripped and stripped.endswith(';'):
                in_import_block = False
            continue

        # Handle multi-line export blocks: export { ... }
        if in_export_block:
            if stripped.endswith('}') or stripped.endswith('};'):
                in_export_block = False
            continue

        if stripped.startswith('import {'):
            # Single-line: import { x } from './y.js';
            if 'from ' in stripped and stripped.endswith(';'):
                continue
            in_import_block = True
            continue

        if stripped.startswith('export {') and not stripped.endswith('}'):
            # multi-line export { ... }
            if '}' not in stripped:
                in_export_block = True
                continue
            continue

        if stripped.startswith('export {') and (stripped.endswith('}') or stripped.endswith('};')):
            continue

        if stripped.startswith('export type {'):
            continue

        # Strip single-line imports
        if stripped.startswith('import ') and ('from ' in stripped or stripped.endswith(';')):
            continue

        # Strip export type
        if stripped.startswith('export type '):
            continue

        # Replace export const/function/class/interface with const/function/class/interface
        if stripped.startswith('export '):
            line = line.replace('export ', '', 1)

        # Strip re-export aliases: `const X = __X__;` left over from
        # import rename bindings (e.g. `import { X as __X__ }`). The
        # source module's content is already inlined so re-export is
        # unnecessary and the __-prefixed variable is now undefined.
        if re.match(r'^const\s+(\w+)\s*=\s*__\1__\s*;\s*$', line.strip()):
            continue

        filtered.append(line)

    js = '\n'.join(filtered)

    # QJSEngine compatibility: optional catch binding -> catch(e)
    js = re.sub(r'\bcatch\s*\{', 'catch(e) {', js)
    # QJSEngine compatibility: strip async/await (parsing only, never executed)
    js = re.sub(r'\basync\s+', '', js)
    js = re.sub(r'\bawait\s+', '', js)

    # QJSEngine compatibility: convert template literals to string concatenation
    js = _convert_template_literals(js)

    # QJSEngine compatibility: replace optional chaining (?.) with .
    # Parsing-only: the extracted function source will be slightly different but
    # functionally equivalent for scriptlet injection purposes.
    # Handle ?.[] first (optional chaining with bracket notation) -> []
    js = js.replace('?.[', '[')
    # Then handle ?.prop -> .prop
    js = js.replace('?.', '.')

    # QJSEngine compatibility: replace nullish coalescing (??) with ||
    # Same reasoning as above: parsing-only, never executed.
    js = js.replace('??', '||')

    # QJSEngine compatibility: replace logical OR assignment (||=) with = ... ||
    # QJSEngine does not support ||= (ES2021).
    js = re.sub(r'(\w+)\s*\|\|=\s*', r'\1 = \1 || ', js)

    # QJSEngine compatibility: replace # private members with _
    # QJSEngine does not support ES2022 private class methods (#method).
    # Both the definition ( #method() {} ) and call site ( this.#method() )
    # use the same identifier, so renaming to _method preserves behavior.
    # Match # preceded by whitespace (definition) or . (call site).
    # Also skip # in strings since (?<=[\s.]) prevents matching inside quotes.
    js = re.sub(r'(?<=[\s.])#(\w+)', r'_\1', js)

    # QJSEngine compatibility: static class fields -> static getters
    # QJSEngine does not support `static X = Y;` (ES2022 class fields)
    js = re.sub(r'^(\s*)static\s+(\w+)\s*=\s*([^;]+?)\s*;',
                lambda m: f'{m.group(1)}static get {m.group(2)}() {{ return {m.group(3)}; }}',
                js, flags=re.MULTILINE)
    # Instance class fields (x = y; inside class body) -> getters
    # MUST run after # -> _ conversion so private fields (#x) are visible as _x
    js = _strip_class_fields(js)

    return js


def _preprocess_scriptlets_js(content: str) -> str:
    """Pre-process scriptlets.js ES module for QJSEngine evaluation.

    Downloads and inlines all submodule imports, strips import/export
    statements, and adds bridge stubs to capture scriptlet registrations.
    """
    # --- Phase 1: Collect and download all submodule imports ---
    submodules = _collect_all_import_content(content)

    # --- Phase 2: Preprocess main content (strip imports/exports) ---
    main_js = _strip_module_imports(content)

    # Remove the 'const builtinScriptlets = registeredScriptlets;' line
    # from main content since it's handled by our stubs.
    main_js = '\n'.join(
        line for line in main_js.split('\n')
        if 'builtinScriptlets' not in line or '= registeredScriptlets' not in line
    )

    # --- Phase 3: Preprocess all submodules ---
    # Order: utility files first (no side effects, only define functions),
    # then submodule files (register scriptlets via registerScriptlet()).
    # Utility files that are also imported by submodules for their functions:
    utility_files = [
        # External files (parent dir) must come before shared.js since
        # shared.js re-exports their classes/functions.
        '../arglist-parser.js', '../jsonpath.js', '../urlskip.js',
        'safe-self.js', 'utils.js', 'run-at.js', 'proxy-apply.js',
        'set-constant.js', 'cookie.js', 'localstorage.js', 'stack-trace.js',
        'shared.js', 'parse-replace.js',
    ]
    # Submodule files that register scriptlets as side effects.
    # Only include files that were actually downloaded.
    submodule_files = sorted(
        f for f in submodules
        if f not in utility_files and f != 'base.js'
    )

    module_parts: list[str] = []

    # Utility files first (define functions that submodules use)
    for filename in utility_files:
        if filename in submodules:
            processed = _strip_module_imports(submodules[filename])
            if processed.strip():
                module_parts.append(processed)

    # Then submodule files (register scriptlets via registerScriptlet())
    for filename in submodule_files:
        if filename in submodules:
            processed = _strip_module_imports(submodules[filename])
            if processed.strip():
                module_parts.append(processed)

    # --- Phase 4: Build expanded stubs with registerScriptlet ---
    # We provide registerScriptlet ourselves (instead of loading base.js)
    # and hook registeredScriptlets.push to capture registrations.
    stubs = '''
var registeredScriptlets = [];
var builtinScriptlets = [];
var scriptletGlobals = { canDebug: false };

// registerScriptlet — replicates upstream base.js behavior AND notifies bridge.
// Used by submodule scriptlets (imported from ./base.js, stripped here).
function registerScriptlet(fn, details) {
    if (typeof details !== 'object') { throw new ReferenceError('Missing scriptlet details'); }
    details.fn = fn;
    fn.details = details;
    if (Array.isArray(details.dependencies)) {
        details.dependencies.forEach(function(fn, i, array) {
            if (typeof fn !== 'function') { return; }
            if (fn.details === undefined) { return; }
            array[i] = fn.details.name;
        });
    }
    registeredScriptlets.push(details);
}

// Hook registeredScriptlets.push — catches BOTH registerScriptlet() pushes
// AND builtinScriptlets.push() (since builtinScriptlets = registeredScriptlets).
const _origScriptletPush = registeredScriptlets.push;
registeredScriptlets.push = function(item) {
    if (item && typeof item === 'object' && item.name && item.fn) {
        Bridge.registerScriptlet(item.name, item.fn);
        if (item.aliases) {
            Bridge.registerAliases(item.name, item.aliases);
        }
    }
    return _origScriptletPush.call(this, item);
};

builtinScriptlets.push = function(item) {
    if (item && typeof item === 'object' && item.name && item.fn) {
        Bridge.registerScriptlet(item.name, item.fn);
        if (item.aliases) {
            Bridge.registerAliases(item.name, item.aliases);
        }
    }
};
'''

    # --- Phase 5: Assemble ---
    parts = [stubs]
    parts.extend(module_parts)
    parts.append(main_js)

    return '\n\n'.join(parts)


# ---- Text-based scriptlet source extraction ----
# QJSEngine's Function.prototype.toString() returns [native code], so we
# extract function/class source directly from the preprocessed JS text.

_BRACKETS_RE = re.compile(r'^(\s*)([\w$]+)\s*=\s*(?:async\s*)?function')


_KEYWORDS_BEFORE_REGEX = frozenset({
    'return', 'typeof', 'void', 'delete', 'throw',
    'new', 'case', 'in', 'instanceof', 'do',
    'else', 'yield', 'await',
})


def _is_regex_start(code: str, slash_pos: int) -> bool:
    """Determine if ``/`` at *slash_pos* starts a regex or is a division operator.

    Uses expression–context heuristics by examining the last significant
    (non‑whitespace) character before the slash.  After identifiers, ``)``,
    ``]``, or number literals the slash is treated as division; after
    operators, opening brackets, or statement‑start keywords it is treated
    as a regex literal.
    """
    i = slash_pos - 1
    while i >= 0 and code[i] in ' \t\n\r':
        i -= 1
    if i < 0:
        return True  # start of file → regex

    ch = code[i]

    # After ) or ] → almost always division
    if ch in ')]':
        return False

    # After an identifier or number literal → check the keyword
    if ch.isalnum() or ch in '_$':
        j = i
        while j >= 0 and (code[j].isalnum() or code[j] in '_$'):
            j -= 1
        word = code[j + 1:i + 1]
        if word in _KEYWORDS_BEFORE_REGEX:
            return True
        return False

    # After } → ambiguous (object literal vs end of block); treat as regex
    # since this is much more common in the uBlock scriptlets source.
    if ch == '}':
        return True

    # Operators, opening brackets, comma, semicolon, colon, etc. → regex
    return True


def _extract_brace_block(code: str, open_pos: int) -> str | None:  # noqa: C901
    """Extract a balanced brace ``{...}`` block starting at or before *open_pos*.

    Handles strings, regex literals, ``//`` and ``/* */`` comments to avoid
    false brace matching inside literals and comments.  Regex–vs–division
    disambiguation uses :func:`_is_regex_start`.
    """
    brace_pos = code.find('{', open_pos)
    if brace_pos == -1:
        return None
    depth = 0
    in_string = False
    in_regex = False
    in_line_comment = False
    in_block_comment = False
    string_char: str | None = None
    pos = brace_pos
    while pos < len(code):
        ch = code[pos]

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            pos += 1
            continue

        if in_block_comment:
            if ch == '*' and pos + 1 < len(code) and code[pos + 1] == '/':
                in_block_comment = False
                pos += 2
                continue
            pos += 1
            continue

        if in_string:
            if ch == '\\':
                pos += 2
                continue
            if ch == string_char:
                in_string = False
            pos += 1
            continue

        if in_regex:
            if ch == '\\':
                pos += 2
                continue
            if ch == '/' and code[pos - 1] != '\\':
                in_regex = False
            pos += 1
            continue

        if ch == '/':
            next_ch = code[pos + 1] if pos + 1 < len(code) else ''
            if next_ch == '/':
                in_line_comment = True
                pos += 2
                continue
            if next_ch == '*':
                in_block_comment = True
                pos += 2
                continue
            if _is_regex_start(code, pos):
                in_regex = True
                pos += 1
                continue

        if ch in '"\'':
            in_string = True
            string_char = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return code[brace_pos:pos + 1]

        pos += 1
    return None


def _extract_string_literal(code: str, pos: int) -> str | None:
    """Extract a quoted string literal starting at pos (must be ' or ")."""
    if pos >= len(code) or code[pos] not in '"\'':
        return None
    quote = code[pos]
    end = pos + 1
    while end < len(code):
        if code[end] == '\\':
            end += 2
            continue
        if code[end] == quote:
            return code[pos:end + 1]
        end += 1
    return None


_PREAMBLE_LINE_LIMIT = 5000


def _extract_registrations(js_code: str) -> list[dict]:
    """Parse all scriptlet registrations from preprocessed JS text.

    Handles both registration patterns:
      - registerScriptlet(fn_var, { name: ..., ... })
      - builtinScriptlets.push({ name: ..., fn: ..., ... })

    Returns list of dicts with keys: name, aliases, fn_var, dependencies
    """
    registrations: list[dict] = []
    seen_names: set[str] = set()

    # Pattern 1: registerScriptlet(fn_var, { ... })
    for m in re.finditer(r'registerScriptlet\(', js_code):
        start = m.start()
        if start < _PREAMBLE_LINE_LIMIT:
            continue  # skip preamble with stub definitions
        after_paren = js_code.index('(', start) + 1
        fn_m = re.match(r'\s*([\w$]+)\s*,\s*', js_code[after_paren:])
        if not fn_m:
            continue
        fn_var = fn_m.group(1)
        block_start = after_paren + fn_m.end()
        block = _extract_brace_block(js_code, block_start)
        if not block:
            continue
        entry = _parse_reg_obj(block, fn_var)
        if entry and entry['name'] not in seen_names:
            seen_names.add(entry['name'])
            registrations.append(entry)

    # Pattern 2: builtinScriptlets.push({ ... })
    for m in re.finditer(r'builtinScriptlets\.push\(\{', js_code):
        block = _extract_brace_block(js_code, m.start())
        if not block:
            continue
        entry = _parse_reg_obj(block, None)
        if entry and entry['name'] not in seen_names:
            seen_names.add(entry['name'])
            registrations.append(entry)

    return registrations


def _parse_reg_obj(block_text: str, default_fn_var: str | None) -> dict | None:
    """Parse a registration object literal block.

    Extracts name, aliases, fn_var, dependencies from:
      { name: 'foo', fn: someVar, aliases: [...], dependencies: [...] }
    """
    # name (required)
    name = _extract_simple_field(block_text, 'name')
    if not name:
        return None

    # fn_var
    fn_var = default_fn_var
    if fn_var is None:
        fn_var = _extract_simple_field(block_text, 'fn')
    if not fn_var:
        return None

    # aliases
    aliases = _extract_array_field(block_text, 'aliases')

    # dependencies (raw — may contain variable references)
    deps = _extract_array_field(block_text, 'dependencies', resolve_strings_only=False)

    return {
        'name': name,
        'aliases': aliases,
        'fn_var': fn_var,
        'dependencies': deps,
    }


def _extract_simple_field(text: str, field: str) -> str | None:
    """Extract a simple string or identifier field value.

    Handles: field: 'value' or field: identifier
    """
    m = re.search(r'(?:^|[\s,])' + re.escape(field) + r'\s*:\s*', text)
    if not m:
        return None
    pos = m.end()
    if pos >= len(text):
        return None
    if text[pos] in '"\'':
        lit = _extract_string_literal(text, pos)
        if lit:
            return lit[1:-1]  # strip quotes
        return None
    # Identifier
    ident = re.match(r'[\w$]+', text[pos:])
    if ident:
        return ident.group()
    return None


def _extract_array_field(text: str, field: str, resolve_strings_only: bool = True) -> list:
    """Extract an array field value.

    If resolve_strings_only is True, only string literals are extracted.
    Otherwise, both strings and identifiers are extracted.
    """
    m = re.search(r'(?:^|[\s,])' + re.escape(field) + r'\s*:\s*', text)
    if not m:
        return []
    pos = m.end()
    if pos >= len(text) or text[pos] != '[':
        return []
    # Skip opening bracket
    pos += 1
    values: list = []
    while pos < len(text):
        ch = text[pos]
        if ch in ' \t\n\r,':
            pos += 1
            continue
        if ch == ']':
            break
        if ch in '"\'':
            lit = _extract_string_literal(text, pos)
            if lit:
                values.append(lit[1:-1])
                pos += len(lit)
            else:
                pos += 1
        elif not resolve_strings_only:
            # Identifier (variable reference)
            ident = re.match(r'[\w$]+', text[pos:])
            if ident:
                values.append(ident.group())
                pos += ident.end()
            else:
                pos += 1
        else:
            pos += 1
    return values


def _extract_fn_source(js_code: str, fn_var: str) -> str | None:
    """Extract the source of a function or class by its variable name.

    Searches for:
      function <fn_var>(...) { ... }
      class <fn_var> { ... }
      const <fn_var> = function(...
      const <fn_var> = ( ... ) =>
    """
    patterns = [
        # function declaration
        rf'function\s+{re.escape(fn_var)}\s*\(',
        # class declaration
        rf'class\s+{re.escape(fn_var)}\b',
        # const/let/var = function
        rf'(?:const|let|var)\s+{re.escape(fn_var)}\s*=\s*(?:async\s*)?function\s*\(',
        # const/let/var = (arrow function)
        rf'(?:const|let|var)\s+{re.escape(fn_var)}\s*=\s*\(',
        # const/let/var = (identifier) => (just to find location)
        rf'(?:const|let|var)\s+{re.escape(fn_var)}\s*=',
    ]
    for pattern in patterns:
        m = re.search(pattern, js_code)
        if m:
            start = m.start()
            # Find the opening brace of the body
            if 'function' in pattern or 'class' in pattern:
                # After the function/class keyword, find the opening paren then brace
                body_start = js_code.index(m.group(), start) + len(m.group())
                # Find opening brace
                brace_pos = js_code.find('{', body_start)
                if brace_pos == -1:
                    continue
                block = _extract_brace_block(js_code, brace_pos)
                if block:
                    # Return everything from function/class keyword to end of block
                    return js_code[start:brace_pos + len(block)]
            elif '=>' in pattern:
                # Arrow function
                body_start = m.end()
                brace_pos = js_code.find('{', body_start)
                if brace_pos == -1:
                    continue
                block = _extract_brace_block(js_code, brace_pos)
                if block:
                    return js_code[start:brace_pos + len(block)]
            else:
                # General expression assignment
                after_eq = js_code[m.end():]
                stripped = after_eq.lstrip()
                if stripped.startswith('function') or stripped.startswith('async function') or stripped.startswith('class'):
                    body_start = m.end() + (len(after_eq) - len(stripped))
                    brace_pos = js_code.find('{', body_start)
                    if brace_pos == -1:
                        continue
                    block = _extract_brace_block(js_code, brace_pos)
                    if block:
                        return js_code[start:brace_pos + len(block)]
    return None


def _resolve_deps(raw_deps: list, fn_to_name: dict[str, str]) -> list[str]:
    """Resolve dependency entries that may be variable references to string names."""
    resolved: list[str] = []
    for dep in raw_deps:
        if isinstance(dep, str) and dep in fn_to_name:
            resolved.append(fn_to_name[dep])
        elif isinstance(dep, str):
            resolved.append(dep)
    return resolved


def _extract_text_sources(js_code: str) -> dict[str, str]:
    """Extract function/class source for all registered scriptlets from JS text.

    Returns dict mapping scriptlet name -> source code string.
    Falls back to text-based extraction when QJSEngine toString() is unavailable.
    """
    registrations = _extract_registrations(js_code)
    if not registrations:
        return {}

    # Build name -> source mapping
    name_to_source: dict[str, str] = {}
    for reg in registrations:
        source = _extract_fn_source(js_code, reg['fn_var'])
        if source:
            name_to_source[reg['name']] = source
        else:
            # Try alternative: extract the entire registration block
            # as fallback for unusual patterns
            pass

    return name_to_source


def _parse_scriptlets_qjs(content: str) -> list[Resource]:
    """Parse scriptlets.js using QJSEngine to handle ES module format.

    Falls back to text-only extraction if QJSEngine is unavailable.
    """
    if not _QJS_AVAILABLE:
        logger.error("braveadblock: QJSEngine not available, cannot parse scriptlets")
        return []

    try:
        app = QCoreApplication.instance()
        if app is None:
            app = QCoreApplication([])

        engine = QJSEngine()
        bridge = ScriptletBridge(engine)
        js_bridge = engine.newQObject(bridge)
        engine.globalObject().setProperty('Bridge', js_bridge)

        js_code = _preprocess_scriptlets_js(content)

        result = engine.evaluate(js_code, 'scriptlets.js')
        if result.isError():
            logger.error(
                "braveadblock: QJSEngine scriptlet parse error: %s",
                result.toString()
            )
            return []

        resources = bridge.resources

        # QJSEngine's Function.prototype.toString() returns [native code],
        # so we extract real function sources from the preprocessed JS text.
        text_sources = _extract_text_sources(js_code)
        for r in resources:
            if r.name in text_sources:
                source_text = text_sources[r.name]
                r.content = base64.b64encode(source_text.encode()).decode("utf-8")

        return resources

    except ImportError:
        logger.error("braveadblock: QJSEngine not available, cannot parse scriptlets")
        return []
    except RuntimeError as e:
        logger.error("braveadblock: QJSEngine runtime error: %s", e)
        return []


_TOP_COMMENT_RE = r"^/\*[\S\s]+?\n\*/\s*"
_NON_EMPTY_LINE_RE = r"\S"


def _parse_scriptlets(
    fileobj: IO[bytes],
) -> List[Resource]:
    with io.TextIOWrapper(fileobj, encoding="utf-8") as text_io:
        lines = re.sub(_TOP_COMMENT_RE, "", text_io.read()).split("\n")

    resources: List[Resource] = []
    current_resource = None
    for line in lines:
        if line.startswith("#") or line.startswith("// ") or line == "//":
            continue

        if current_resource is None:
            if line.startswith("/// "):
                name = line[4:].strip()
                current_resource = Resource(name, [], "", "")
            continue

        if line.startswith("/// "):
            tokens = line[4:].split()
            if len(tokens) >= 2 and tokens[0] == "alias":
                current_resource.aliases.append(tokens[1])
            continue

        if re.search(_NON_EMPTY_LINE_RE, line):
            current_resource.content += line.rstrip() + "\n"
            continue

        if "{{1}}" in current_resource.content:
            current_resource.content_type = "template"
        else:
            current_resource.content_type = "application/javascript"

        current_resource.content = base64.b64encode(
            current_resource.content.encode()
        ).decode("utf-8")

        resources.append(current_resource)
        current_resource = None

    return resources


def _on_resource_download(
    resources: List[Resource],
    aliases_map: Dict[str, List[str]],
    url: QUrl,
    fileobj: IO[bytes],
) -> None:
    """On downloading a resource, parse it and add append it to the resources list.

    If the url is the scriptlets url, then parse that differently.
    """
    try:
        if url == _SCRIPTLETS_URL:
            content = fileobj.read().decode("utf-8")
            resources.extend(_parse_scriptlets_qjs(content))
        else:
            resources.append(_parse_resource(aliases_map, url, fileobj))
    except UnicodeDecodeError:
        message.info(f"braveadblock: Resource from {url} is not valid utf-8")
    except ValueError:
        message.info(f"braveadblock: Could not parse resource from {url}")


def _on_all_resources_downloaded(
    resources: List[Resource],
    engine_info: EngineInfo,
    done_count: int,
) -> None:
    """After downloading all resources, create the cache and update the engine."""
    _serialize_resources_to_file(resources, engine_info.resources_cache_path)
    batch = [
        (r.name, r.content_type, r.content, r.aliases)
        for r in resources
    ]
    engine_info.engine.add_resources(batch)
    engine_info.engine.serialize_to_file(str(engine_info.cache_path))
    redirect_count = done_count - 1  # exclude scriptlets.js from download count
    scriptlet_count = len(resources) - redirect_count
    message.info(
        f"braveadblock: Resource cache updated — {len(resources)} resources "
        f"({redirect_count} redirect + {scriptlet_count} scriptlets)."
    )


_REDIRECTABLE_RESOURCES_DECL = "export default new Map(["
_MAP_END_RE = r"^\s*\]\s*\)"


def _on_redirect_engine_download(  # noqa: C901
    _url: QUrl, fileobj: IO[bytes], engine_info: EngineInfo
) -> Optional[blockutils.BlocklistDownloads]:
    """Download redirect-resources.js, parse it, and get the list of resource urls.

    To parse it, we take the portion that refers to the map, and turn it into JSON, and
    decode with json.load. The inspiration for this code comes from
    https://github.com/brave/adblock-rust/blob/master/src/resources/resource_assembler.rs

    Since this is almost a one-to-one mapping of the code in the above file, it's
    probably to keep everything in one function (hence the noqa: C901).
    """
    try:
        # Extract the lines for the map
        map_lines = []
        map_opening_seen = False
        with io.TextIOWrapper(fileobj, encoding="utf-8") as text_io:
            for line in text_io.readlines():
                if not map_opening_seen:
                    if _REDIRECTABLE_RESOURCES_DECL in line:
                        map_opening_seen = True
                        map_lines.append("[")
                elif re.search(_MAP_END_RE, line):
                    map_lines.append("]")
                    break
                else:
                    map_lines.append(line)

                if map_lines:
                    comment_idx = map_lines[-1].find("//")
                    if comment_idx >= 0:
                        map_lines[-1] = map_lines[-1][:comment_idx]

        # Get rid of trailing block comments
        map_lines = [re.sub(r"/\*[^'\"]*\*/\s*$", "", line) for line in map_lines]

        # Get rid of all whitespace
        map_str = re.sub(r"\s+", "", "".join(map_lines))

        # Replace all ' with "
        map_str = map_str.replace("'", '"')

        # Get rid of all trailing commas
        map_str = re.sub(r",([\],\}])", r"\1", map_str)

        # Replace all property keys directly preceded by a `{` or a `,` and followed by
        # a `:` with double-quoted versions.
        map_str = re.sub(r"([\{,])([a-zA-Z][a-zA-Z0-9_]*):", r'\1"\2":', map_str)

        # Read via json
        resources_map = json.loads(map_str)

        resource_urls: List[QUrl] = []
        # Mapping from resource url (in string form) to list of aliases
        aliases_map: Dict[str, List[str]] = {}
        for resource in resources_map:
            name, data = resource
            # Skip any resource with params since we don't know how to deal with it
            if "params" in data:
                continue

            url_str = _WEB_ACCESSIBLE_RESOURCES_URL_TEMPLATE.format(name)
            if "alias" in data:
                if isinstance(data["alias"], list):
                    aliases_map[url_str] = data["alias"]
                else:
                    aliases_map[url_str] = [data["alias"]]
            else:
                aliases_map[url_str] = []

            resource_urls.append(QUrl(url_str))

        resources: List[Resource] = []
        dl = blockutils.BlocklistDownloads(resource_urls + [_SCRIPTLETS_URL])
        dl.single_download_finished.connect(
            functools.partial(_on_resource_download, resources, aliases_map)
        )
        dl.all_downloads_finished.connect(
            functools.partial(
                _on_all_resources_downloaded,
                resources,
                engine_info,
            )
        )
        dl.initiate()

        return dl

    except UnicodeDecodeError:
        message.error("braveadblock: redirect-resources.js is not in valid utf-8")
    except (TypeError, json.JSONDecodeError):
        message.error("braveadblock: redirect-resources.js could not be parsed")

    return None


def download_resources(engine_info: EngineInfo) -> blockutils.BlocklistDownloads:
    """Download ublock origin type resources and add them to given engine.

    Also updates the engine's cache path and resources cache path while we're at it.
    """
    dl = blockutils.BlocklistDownloads([_REDIRECT_RESOURCES_URL])
    dl.single_download_finished.connect(
        functools.partial(_on_redirect_engine_download, engine_info=engine_info)
    )
    dl.initiate()
    return dl
