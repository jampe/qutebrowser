# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bitwarden component POC.

Page integration only: content-script field discovery, an inline menu, login
fill, page-load autofill and TOTP. Vault access goes through the ``bw`` CLI (or
a fake JSON vault for tests/demos).
"""

import functools
import itertools
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from qutebrowser.api import apitypes, cmdutils, hook, message, messaging
from qutebrowser.qt.core import QObject, QTimer, pyqtSignal
from qutebrowser.utils import log, resources, usertypes
from qutebrowser.utils import message as qmessage

CHANNEL = 'bitwarden'
CONTENT_SCRIPT = 'bitwarden:content'


class VaultError(Exception):
    pass


@dataclass
class VaultItem:
    id: str
    name: str
    username: str
    password: str
    totp: str | None = None
    uris: list[str] = field(default_factory=list)


def _normalize_item(raw: dict[str, Any]) -> VaultItem | None:
    login = raw.get('login') or {}
    if not isinstance(login, dict):
        return None
    return VaultItem(
        id=str(raw.get('id', '')),
        name=str(raw.get('name', '')),
        username=str(login.get('username') or ''),
        password=str(login.get('password') or ''),
        totp=login.get('totp') or None,
        uris=[str(uri.get('uri', '')) for uri in raw.get('uris', [])
              if isinstance(uri, dict) and uri.get('uri')],
    )


def _hosts_match(candidate: str, target: str) -> bool:
    candidate_host = urlparse(candidate if '://' in candidate
                              else 'https://' + candidate).hostname or ''
    target_host = urlparse(target if '://' in target
                           else 'https://' + target).hostname or ''
    if not candidate_host or not target_host:
        return False
    return (candidate_host == target_host
            or candidate_host.endswith('.' + target_host)
            or target_host.endswith('.' + candidate_host))


class FakeVault:

    def __init__(self, items: list[VaultItem]) -> None:
        self._items = {item.id: item for item in items}
        self._unlocked = True
        self.last_password: str | None = None

    @classmethod
    def from_file(cls, path: str) -> 'FakeVault':
        with open(path, encoding='utf-8') as fobj:
            raw = json.load(fobj)
        items = [_normalize_item(item) for item in raw]
        return cls([item for item in items if item is not None])

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    def unlock(self, password: str | None = None) -> bool:
        self.last_password = password
        self._unlocked = True
        return True

    def list_items(self, url: str) -> list[VaultItem]:
        return [
            item for item in self._items.values()
            if not item.uris or any(_hosts_match(uri, url) for uri in item.uris)
        ]

    def get_item(self, item_id: str) -> VaultItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise VaultError(f"No such item: {item_id}")

    def get_totp(self, item_id: str) -> str | None:
        return self.get_item(item_id).totp

    def sync(self) -> None:
        pass

    def lock(self) -> None:
        self._unlocked = False


def _is_session_error(error: str) -> bool:
    lowered = error.lower()
    return any(token in lowered for token in (
        'invalid session', 'session key', 'vault is locked', 'not logged in'))


class BwCliVault:

    def __init__(self) -> None:
        self._bw = shutil.which('bw')
        self._session = os.environ.get('BW_SESSION') or None
        self._session_from_env = self._session is not None

    @property
    def unlocked(self) -> bool:
        return self._session is not None

    def _run(self, *args: str) -> str:
        if self._bw is None:
            raise VaultError("Bitwarden CLI ('bw') not found")
        process = subprocess.run(
            [self._bw, *args],
            text=True, capture_output=True, check=False)
        if process.returncode:
            error = process.stderr.strip() or process.stdout.strip()
            if self._session is not None and _is_session_error(error):
                self._session = None
                self._session_from_env = False
            raise VaultError(error or "Bitwarden CLI failed")
        return process.stdout

    def unlock(self, password: str | None = None) -> bool:
        if password is None:
            raise VaultError("No master password")
        env = {**os.environ, 'BW_MASTERPASS': password}
        if self._bw is None:
            raise VaultError("Bitwarden CLI ('bw') not found")
        process = subprocess.run(
            [self._bw, 'unlock', '--raw', '--passwordenv', 'BW_MASTERPASS'],
            env=env, text=True, capture_output=True, check=False)
        if process.returncode:
            raise VaultError(process.stderr.strip() or "Unlock failed")
        self._session = process.stdout.strip()
        self._session_from_env = False
        return bool(self._session)

    def _session_args(self) -> list[str]:
        if self._session is None:
            raise VaultError("Vault is locked")
        return ['--nointeraction', '--session', self._session]

    def list_items(self, url: str) -> list[VaultItem]:
        output = self._run(
            'list', 'items', *self._session_args(), '--url', url)
        raw = json.loads(output)
        items = [_normalize_item(item) for item in raw]
        return [item for item in items
                if item is not None and (item.username or item.password)]

    def get_item(self, item_id: str) -> VaultItem:
        output = self._run('get', 'item', item_id, *self._session_args())
        item = _normalize_item(json.loads(output))
        if item is None:
            raise VaultError("Item has no login")
        return item

    def get_totp(self, item_id: str) -> str | None:
        return self._run('get', 'totp', item_id, *self._session_args()).strip()

    def sync(self) -> None:
        self._run('sync', *self._session_args())

    def lock(self) -> None:
        self._session = None
        self._session_from_env = False
        if self._bw is not None:
            try:
                self._run('lock')
            except VaultError:
                pass


class _AsyncSignals(QObject):
    done = pyqtSignal(int, object)
    error = pyqtSignal(int, str)


class _AsyncRunner(QObject):

    """Run vault calls off the GUI thread and deliver results on the GUI thread."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _AsyncSignals(self)
        self._signals.done.connect(self._on_done)
        self._signals.error.connect(self._on_error)
        self._callbacks: dict[int, Callable[[Any, str | None], None]] = {}
        self._ids = itertools.count()

    def submit(self, func: Callable[[], Any],
               callback: Callable[[Any, str | None], None]) -> None:
        request_id = next(self._ids)
        self._callbacks[request_id] = callback

        def run() -> None:
            try:
                result = func()
            except Exception as e:  # noqa: BLE001
                self._signals.error.emit(request_id, str(e))
            else:
                self._signals.done.emit(request_id, result)

        threading.Thread(target=run, daemon=True).start()

    def _on_done(self, request_id: int, result: Any) -> None:
        callback = self._callbacks.pop(request_id, None)
        if callback is not None:
            callback(result, None)

    def _on_error(self, request_id: int, error: str) -> None:
        callback = self._callbacks.pop(request_id, None)
        if callback is not None:
            callback(None, error)


class BitwardenComponent:

    def __init__(self, vault: Any | None = None, *,
                 autofill_on_load: bool = True) -> None:
        self._vault = vault or self._make_vault()
        self._runner = _AsyncRunner()
        self._autofill_on_load = autofill_on_load
        self._last_status = ""

    @staticmethod
    def _make_vault() -> Any:
        path = os.environ.get('QUTE_BITWARDEN_FAKE_VAULT')
        if path:
            return FakeVault.from_file(path)
        return BwCliVault()

    @property
    def vault(self) -> Any:
        return self._vault

    def status(self) -> str:
        name = type(self._vault).__name__
        if getattr(self._vault, 'unlocked', False):
            return f"Bitwarden: unlocked ({name})"
        return f"Bitwarden: locked ({name})"

    def toggle_autofill(self) -> bool:
        self._autofill_on_load = not self._autofill_on_load
        return self._autofill_on_load

    # Hooks

    def before_load(self, tab: apitypes.ExtensionTab, url: Any) -> None:
        if tab.is_private:
            return
        last_url = tab.last_navigation_url
        if last_url is not None and last_url != url:
            # Page-wide scripts are injected once per document; a subframe
            # navigation must not replace the script mid-load.
            return
        tab.add_web_script(
            CONTENT_SCRIPT,
            resources.read_file('javascript/bitwarden/content.js'),
            world=apitypes.JsWorld.application,
            injection_point=apitypes.InjectionPoint.creation,
            subframes=True,
        )

    def on_load_finished(self, tab: apitypes.ExtensionTab, ok: bool) -> None:
        if not ok or tab.is_private or not self._autofill_on_load:
            return
        self._poll_autofill(tab, attempts=0)

    def _poll_autofill(self, tab: apitypes.ExtensionTab,
                       attempts: int) -> None:
        try:
            if tab.messaging_supported:
                tab.run_js_async(
                    "JSON.stringify(window.__bwCollectFields ? "
                    "window.__bwCollectFields() : null)",
                    functools.partial(self._on_collected_fields, tab),
                    world=apitypes.JsWorld.application,
                )
                return
        except Exception:  # noqa: BLE001
            return
        if attempts < 30:
            QTimer.singleShot(
                100, functools.partial(
                    self._poll_autofill, tab, attempts + 1))
        else:
            log.extensions.debug(
                "Bitwarden autofill skipped: messaging not enabled")

    def _on_collected_fields(self, tab: apitypes.ExtensionTab,
                             result: Any) -> None:
        try:
            fields = json.loads(result) if isinstance(result, str) else None
        except (ValueError, TypeError):
            return
        if not isinstance(fields, dict):
            return
        if not (fields.get('username') or fields.get('password')):
            return
        url = fields.get('url') or tab.url().toString()
        self._lookup(url, lambda items, error: self._auto_fill_items(
            tab, url, items, error, frame=None))

    # Messaging

    def on_message(self, message_obj: messaging.Message) -> None:
        if message_obj.channel != CHANNEL:
            return
        payload = message_obj.payload
        if not isinstance(payload, dict):
            return
        message_type = message_obj.type

        if message_type == 'fieldFocus':
            url = payload.get('url') or message_obj.frame.url.toString()
            self._lookup(url, lambda items, error: self._send_candidates(
                message_obj, items, error))
        elif message_type == 'fill':
            item_id = payload.get('item_id')
            if isinstance(item_id, str):
                self._send_credentials(
                    message_obj, item_id, auto=False)
        elif message_type == 'autoFillRequest':
            if not self._autofill_on_load:
                return
            url = payload.get('url') or message_obj.frame.url.toString()
            self._lookup(url, lambda items, error: self._auto_fill_items(
                message_obj.tab, url, items, error, frame=message_obj.frame))
        elif message_type == 'filled':
            log.extensions.debug(
                "Bitwarden fill reported: auto=%s",
                bool(payload.get('auto')))

    def _send_candidates(self, message_obj: messaging.Message,
                         items: list[VaultItem] | None,
                         error: str | None) -> None:
        if error:
            log.extensions.warning("Bitwarden lookup failed: %s", error)
            return
        summaries = [
            {'id': item.id, 'name': item.name, 'username': item.username}
            for item in items or []
        ]
        self._send(message_obj.tab,
                   {'type': 'candidates', 'items': summaries},
                   message_obj.frame)

    def _send_credentials(self, message_obj: messaging.Message,
                          item_id: str, *, auto: bool) -> None:
        self._run(
            lambda: self._vault.get_item(item_id),
            functools.partial(
                self._on_item, message_obj, auto=auto))

    def _on_item(self, message_obj: messaging.Message, item: Any,
                 error: str | None, *, auto: bool) -> None:
        if error or item is None:
            log.extensions.warning("Bitwarden item lookup failed: %s", error)
            return
        totp = None
        if item.totp:
            self._run(
                lambda: self._vault.get_totp(item.id),
                functools.partial(
                    self._on_credentials_ready, message_obj, item, auto=auto))
            return
        self._deliver_credentials(message_obj, item, totp, auto=auto)

    def _on_credentials_ready(self, message_obj: messaging.Message,
                              item: Any, totp: Any,
                              error: str | None, *, auto: bool) -> None:
        self._deliver_credentials(message_obj, item, totp, auto=auto)

    def _deliver_credentials(self, message_obj: messaging.Message,
                             item: VaultItem, totp: str | None,
                             *, auto: bool) -> None:
        self._send(message_obj.tab, {
            'type': 'credentials',
            'auto': auto,
            'username': item.username,
            'password': item.password,
            'totp': totp,
        }, message_obj.frame)

    def _auto_fill_items(self, tab: apitypes.ExtensionTab, url: str,
                         items: list[VaultItem] | None,
                         error: str | None, *,
                         frame: Any) -> None:
        log.extensions.debug("Bitwarden auto candidates=%d error=%s",
                             len(items or []), error)
        if error or not items or len(items) != 1:
            return
        self._run(
            lambda: self._vault.get_item(items[0].id),
            functools.partial(
                self._on_auto_item, tab, frame))

    def _on_auto_item(self, tab: apitypes.ExtensionTab, frame: Any,
                      item: Any, error: str | None) -> None:
        if error or item is None:
            return
        if item.totp:
            self._run(
                lambda: self._vault.get_totp(item.id),
                functools.partial(
                    self._on_auto_totp, tab, frame, item))
        else:
            self._send_auto(tab, frame, item, None)

    def _on_auto_totp(self, tab: apitypes.ExtensionTab, frame: Any,
                      item: VaultItem, totp: Any,
                      error: str | None) -> None:
        self._send_auto(tab, frame, item, totp)

    def _send_auto(self, tab: apitypes.ExtensionTab, frame: Any,
                   item: VaultItem, totp: str | None) -> None:
        self._send(tab, {
            'type': 'credentials',
            'auto': True,
            'username': item.username,
            'password': item.password,
            'totp': totp,
        }, frame)

    def _send(self, tab: apitypes.ExtensionTab, payload: dict[str, Any],
              frame: Any) -> None:
        envelope = {
            'channel': CHANNEL,
            'type': payload.get('type'),
            'payload': payload,
        }
        log.extensions.debug("Bitwarden sending type=%s", payload.get('type'))
        try:
            tab.send_message(envelope, frame=frame)
        except Exception as e:  # noqa: BLE001
            log.extensions.warning("Bitwarden could not send message: %s", e)

    # Vault helpers

    def _ensure_unlocked(
            self, callback: Callable[[bool, str | None], None]) -> None:
        if getattr(self._vault, 'unlocked', False):
            callback(True, None)
            return

        def on_password(password: Any) -> None:
            if not password:
                callback(False, "Unlock cancelled")
                return
            self._run(
                lambda: self._vault.unlock(str(password)),
                lambda _result, error: callback(error is None, error))

        self._ask_master_password(on_password)

    def _ask_master_password(
            self, callback: Callable[[str | None], None]) -> None:
        """Ask for the master password only; the CLI session is already logged in."""
        if shutil.which('rofi'):
            process = subprocess.run(
                ['rofi', '-dmenu', '-p', 'Bitwarden Master Password',
                 '-password', '-lines', '0'],
                text=True, capture_output=True, check=False)
            callback(process.stdout.strip() if process.returncode == 0 else None)
            return
        if shutil.which('zenity'):
            process = subprocess.run(
                ['zenity', '--password', '--title',
                 'Bitwarden Master Password'],
                text=True, capture_output=True, check=False)
            callback(process.stdout.strip() if process.returncode == 0 else None)
            return

        def on_answer(answer: Any) -> None:
            callback(getattr(answer, 'password', None))

        # Last resort: qutebrowser has no password-only prompt mode, so use the
        # username/password prompt and ignore the username field.
        qmessage.ask_async(
            "Bitwarden Master Password", usertypes.PromptMode.user_pwd,
            on_answer,
            text="Username is ignored; enter only the master password.")

    def _lookup(self, url: str,
                callback: Callable[[list[VaultItem] | None, str | None],
                                   None], *,
                allow_retry: bool = True) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or parsed.path
        if not host:
            callback([], None)
            return
        log.extensions.debug("Bitwarden lookup host=%s", host)
        self._ensure_unlocked(
            lambda unlocked, error: self._lookup_unlocked(
                host, callback, unlocked, error, allow_retry=allow_retry))

    def _lookup_unlocked(
            self, host: str,
            callback: Callable[[list[VaultItem] | None, str | None], None],
            unlocked: bool, error: str | None, *,
            allow_retry: bool) -> None:
        if not unlocked:
            callback(None, error)
            return

        def on_items(items: list[VaultItem] | None,
                     error: str | None) -> None:
            if error:
                log.extensions.debug(
                    "Bitwarden lookup result count=%d error=%s",
                    len(items or []), error)
            if error and allow_retry and _is_session_error(error):
                self._lookup(host, callback, allow_retry=False)
                return
            callback(items, error)

        self._run(lambda: self._vault.list_items(host), on_items)

    def _run(self, func: Callable[[], Any],
             callback: Callable[[Any, str | None], None]) -> None:
        self._runner.submit(func, callback)


_component: BitwardenComponent | None = None


def _init() -> None:
    global _component
    _component = BitwardenComponent()
    messaging.register(CHANNEL, _component.on_message, allow_relay=True)


def _current() -> BitwardenComponent | None:
    return _component


@hook.init()
def init(context: Any) -> None:
    _init()


@hook.before_load()
def before_load(tab: apitypes.ExtensionTab, url: Any) -> None:
    component = _current()
    if component is not None:
        component.before_load(tab, url)


@hook.load_finished()
def load_finished(tab: apitypes.ExtensionTab, ok: bool) -> None:
    component = _current()
    if component is not None:
        component.on_load_finished(tab, ok)


@cmdutils.register()
@cmdutils.argument('tab', value=cmdutils.Value.cur_tab)
def bitwarden_status(tab: apitypes.Tab) -> None:
    """Show the Bitwarden vault status."""
    component = _current()
    if component is None:
        message.info("Bitwarden component is not initialized")
        return
    message.info(component.status())


@cmdutils.register()
def bitwarden_lock() -> None:
    """Lock the Bitwarden vault."""
    component = _current()
    if component is None:
        return
    component.vault.lock()
    message.info("Bitwarden locked")


@cmdutils.register()
def bitwarden_sync() -> None:
    """Sync the Bitwarden vault."""
    component = _current()
    if component is None:
        return
    component._run(component.vault.sync,
                   lambda _result, error: message.info(
                       error or "Bitwarden synced"))


@cmdutils.register()
def bitwarden_autofill_on_load() -> None:
    """Toggle page-load autofill."""
    component = _current()
    if component is None:
        return
    enabled = component.toggle_autofill()
    message.info("Bitwarden page-load autofill: {}".format(
        "on" if enabled else "off"))


@cmdutils.register()
@cmdutils.argument('tab', value=cmdutils.Value.cur_tab)
def bitwarden_fill(tab: apitypes.Tab) -> None:
    """Pick a login for the current page and fill the main frame."""
    component = _current()
    if component is None:
        return
    adapter = tab.api_adapter()
    if not adapter.messaging_supported:
        message.error("Bitwarden messaging is not enabled")
        return

    def on_items(items: list[VaultItem] | None, error: str | None) -> None:
        if error:
            message.error("Bitwarden lookup failed: {}".format(error))
            return
        if not items:
            message.info("No matching Bitwarden logins")
            return
        lines = ["{} | {}".format(item.name, item.username)
                 for item in items]
        choice = _dmenu(lines)
        if not choice:
            return
        item = items[lines.index(choice)]
        component._run(
            lambda: component.vault.get_item(item.id),
            functools.partial(_fill_picked_item, adapter, component, item))

    component._lookup(adapter.url().toString(), on_items)


def _dmenu(lines: list[str]) -> str | None:
    for command in (['rofi', '-dmenu', '-i', '-p', 'Bitwarden'],
                    ['dmenu', '-i', '-p', 'Bitwarden']):
        if shutil.which(command[0]) is None:
            continue
        process = subprocess.run(
            command, input="\n".join(lines), text=True,
            capture_output=True, check=False)
        if process.returncode == 0:
            return process.stdout.strip() or None
        return None
    message.error("No rofi/dmenu available for Bitwarden picker")
    return None


def _fill_picked_item(adapter: apitypes.ExtensionTab,
                      component: BitwardenComponent, item: VaultItem,
                      _result: Any, error: str | None) -> None:
    if error:
        message.error("Bitwarden item lookup failed: {}".format(error))
        return
    totp = None
    if item.totp:
        try:
            totp = component.vault.get_totp(item.id)
        except VaultError as e:
            log.extensions.warning("Bitwarden TOTP failed: %s", e)
    try:
        adapter.send_message({
            'type': 'credentials',
            'auto': False,
            'username': item.username,
            'password': item.password,
            'totp': totp,
        }, frame=None)
    except Exception as e:  # noqa: BLE001
        message.error("Bitwarden could not fill: {}".format(e))
