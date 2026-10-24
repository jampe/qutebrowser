#!/usr/bin/env node
/*
 * Regression tests for qutebrowser/javascript/procedural-filter.js
 *
 * Verifies the F2 code-quality fixes:
 *  - HIGH-1: matches-css / matches-css-before / matches-css-after translation
 *    matches uBO semantics (regex values stripped of slashes, plain values
 *    anchored as ^escaped$)
 *  - HIGH-2: per-filter error isolation covers CONSTRUCTION (a malformed
 *    xpath/regex filter must not kill the rest of the batch)
 *  - MEDIUM-3: matches-media/watch-attr re-application via the
 *    domFilterer.commit() wiring
 *
 * Run: node tests/unit/javascript/test_procedural_filter.js
 */
'use strict';

var path = require('path');
var ENGINE = path.join(__dirname, '..', '..', '..', 'qutebrowser',
    'javascript', 'procedural-filter.js');

var failures = 0;
var passed = 0;

function check(cond, label) {
    if (cond) {
        passed++;
        console.log('ok: ' + label);
    } else {
        failures++;
        console.error('FAIL: ' + label);
    }
}

function cssSelector(arg) {
    return { type: 'css-selector', arg: arg };
}

function task(type, arg) {
    return { type: type, arg: arg };
}

function action(selectors, actionObj) {
    var obj = { selector: selectors };
    if (actionObj) { obj.action = actionObj; }
    return JSON.stringify(obj);
}

// Fake element node. `parentElement` defaults to a fake body so the
// domWatcher's detached-node filter (parentElement === null) does not drop it.
function makeNode(opts, counters) {
    opts = opts || {};
    return {
        textContent: opts.textContent === undefined ? 'some ad text' : opts.textContent,
        nodeType: 1,
        parentElement: opts.parentElement === null
            ? null : (opts.parentElement || { localName: 'body' }),
        localName: opts.localName || 'div',
        id: opts.id || '',
        _classes: opts.classes || [],
        getAttributeNames: function() { return []; },
        getAttribute: function() { return null; },
        setAttribute: function() { counters.hidden++; },
        removeAttribute: function() { counters.removeAttr++; },
        contains: function() { return false; },
        remove: function() { counters.removed++; },
        previousElementSibling: null,
        nextElementSibling: null,
    };
}

// Supports the selectors used by the engine tests: `tag`, `.class`,
// `tag.class`, `#id`, `tag#id`.
function matchesSelector(node, sel) {
    var m = /^([a-z0-9]*)((?:[.#][\w-]+)*)$/i.exec(sel.trim());
    if (m === null) { return false; }
    if (m[1] !== '' && node.localName !== m[1].toLowerCase()) { return false; }
    var re = /([.#])([\w-]+)/g;
    var part;
    while ((part = re.exec(m[2])) !== null) {
        if (part[1] === '.' && node._classes.indexOf(part[2]) === -1) { return false; }
        if (part[1] === '#' && node.id !== part[2]) { return false; }
    }
    return true;
}

function run(actions, env) {
    // env: { style, media, badXpath, nodes }
    // Counters must be live (getters) — listeners fired after apply() (e.g.
    // matches-media change) mutate them after this function has returned.
    var counters = { hidden: 0, removed: 0, removeAttr: 0, styles: 0 };
    var state = { nodes: [] };
    var specs = env.nodes || [{ localName: 'div', classes: ['ad'] }];
    for (var si = 0; si < specs.length; si++) {
        state.nodes.push(makeNode(specs[si], counters));
    }
    var style = env.style || {};
    var media = env.media || {
        media: 'not all',
        matches: false,
        addEventListener: function(name, fn) { this.listener = fn; }
    };
    global.window = global;
    global.document = {
        head: {
            localName: 'head',
            appendChild: function() { counters.styles++; }
        },
        body: { localName: 'body' },
        documentElement: { localName: 'html', appendChild: function() {} },
        querySelectorAll: function(sel) {
            var out = [];
            for (var ni = 0; ni < state.nodes.length; ni++) {
                if (matchesSelector(state.nodes[ni], sel)) { out.push(state.nodes[ni]); }
            }
            return out;
        },
        createElement: function() {
            return {
                set type(v) {},
                set textContent(v) {},
                appendChild: function() {}
            };
        },
        createExpression: function() {
            if (env.badXpath === true) {
                throw new Error('SyntaxError: invalid xpath');
            }
            return {
                evaluate: function() {
                    return {
                        snapshotLength: 0,
                        snapshotItem: function() { return null; }
                    };
                }
            };
        }
    };
    global.getComputedStyle = function() { return style; };
    global.matchMedia = function() { return media; };
    // MutationObserver shim: remember the callback so fireMutation() can
    // deliver a synthetic record batch. observe()/disconnect() are no-ops.
    global.MutationObserver = function(callback) {
        this._callback = callback;
        global.__mutationObserverCallback = callback;
    };
    global.MutationObserver.prototype.observe = function() {};
    global.MutationObserver.prototype.disconnect = function() {};
    // Deterministic synchronous timers, so the SafeAnimationFrame deferral
    // runs to completion inside fireMutation().
    global.requestAnimationFrame = function(fn) { fn(); return 0; };
    global.cancelAnimationFrame = function() {};
    global.setTimeout = function(fn) { fn(); return 0; };
    global.clearTimeout = function() {};
    global.XPathResult = { UNORDERED_NODE_SNAPSHOT_TYPE: 7 };
    global.self = global;

    delete require.cache[require.resolve(ENGINE)];
    delete global.window._qutebrowser;

    // Expected warnings (bad filters being skipped) would pollute output.
    var origWarn = console.warn;
    console.warn = function() {};
    try {
        require(ENGINE);
        var hits = window._qutebrowser.proceduralFilterer.apply(actions);
    } finally {
        console.warn = origWarn;
    }
    return {
        hits: hits,
        get hidden() { return counters.hidden; },
        get removed() { return counters.removed; },
        get removeAttr() { return counters.removeAttr; },
        get styles() { return counters.styles; },
        nodes: state.nodes,
        media: media,
        // Register a new fake element (returned; also appended to the
        // document registry so querySelectorAll sees it).
        addNode: function(opts) {
            var n = makeNode(opts, counters);
            state.nodes.push(n);
            return n;
        },
        // Deliver a synthetic MutationObserver batch. The synchronous timer
        // stubs make the engine's SafeAnimationFrame deferral run immediately.
        fireMutation: function(addedNodes, removedNodes) {
            global.__mutationObserverCallback([{
                addedNodes: addedNodes || [],
                removedNodes: removedNodes || []
            }]);
        },
        // Re-enter the public API without reloading the engine (persistence).
        applyMore: function(moreActions) {
            return window._qutebrowser.proceduralFilterer.apply(moreActions);
        }
    };
}

// --- HIGH-1: matches-css family translation --------------------------------

var r1 = run([
    action([cssSelector('div'), task('matches-css', 'background-image: /^url/')])
], { style: { 'background-image': 'url("https://x/ad.png")' } });
check(r1.hits === 1 && r1.hidden > 0,
    'matches-css regex value matches computed style (slashes stripped)');

var r2 = run([
    action([cssSelector('div'), task('matches-css', 'background-image: 10px')])
], { style: { 'background-image': '110px' } });
check(r2.hits === 0 && r2.hidden === 0,
    'matches-css plain value anchored: 10px does not match 110px');

var r2b = run([
    action([cssSelector('div'), task('matches-css', 'background-image: 10px')])
], { style: { 'background-image': '10px' } });
check(r2b.hits === 1,
    'matches-css plain value anchored: 10px matches 10px');

var r3 = run([
    action([cssSelector('div'), task('matches-css', 'background-image: /^URL/i')])
], { style: { 'background-image': 'url("https://x/ad.png")' } });
check(r3.hits === 1,
    'matches-css regex flags preserved (i flag)');

var r4 = run([
    action([cssSelector('div'),
        task('matches-css-before', 'content: /^"bar"$/')])
], { style: { content: '"bar"' } });
check(r4.hits === 1,
    'matches-css-before regex value matches');

// --- HIGH-2: per-filter isolation during construction ----------------------

var r5 = run([
    action([cssSelector('div'), task('xpath', '//div[')]),
    action([cssSelector('div'), task('has-text', '/(/')]),
    action([cssSelector('.ad')], { type: 'remove' })
], { badXpath: true });
check(r5.hits === 1 && r5.removed === 1,
    'malformed xpath and regex filters do not kill the good filter');

// --- MEDIUM-3: re-application via domFilterer.commit() wiring ---------------

var r6 = run([
    action([cssSelector('div'), task('matches-media', '(max-width: 100px)')])
], { media: { media: '(max-width: 100px)', matches: false,
              addEventListener: function(name, fn) { this.listener = fn; } } });
check(r6.hits === 0 && r6.hidden === 0,
    'matches-media: no match while media query is false');
r6.media.matches = true;
r6.media.listener();
check(r6.hidden > 0,
    'matches-media re-applies after media change (commit wiring)');

// --- Step 3: domWatcher live re-application --------------------------------

var t1 = run([action([cssSelector('div.late')])], { nodes: [] });
check(t1.hits === 0 && t1.hidden === 0,
    'domWatcher: no match before late insertion');
var t1node = t1.addNode({ localName: 'div', classes: ['late'] });
t1.fireMutation([t1node]);
check(t1.hidden >= 1,
    'domWatcher: late-inserted node hidden without a new apply()');

var t2 = run([action([cssSelector('script')])], { nodes: [] });
var t2node = t2.addNode({ localName: 'script' });
t2.fireMutation([t2node]);
check(t2.hidden === 0,
    'domWatcher: script insertions are ignored (ignoreTags)');

var t3 = run([action([cssSelector('div')])],
    { nodes: [{ localName: 'div' }] });
check(t3.hits === 1 && t3.hidden > 0,
    'domWatcher: initial hide before removal');
var t3node = t3.nodes[0];
t3.nodes.length = 0;
t3.fireMutation([], [t3node]);
check(t3.removeAttr >= 1,
    'domWatcher: removal re-runs commitNow and un-hides stale nodes');

var t4 = run([action([cssSelector('div')])],
    { nodes: [{ localName: 'div' }] });
check(t4.hits === 1,
    'persistent filterer: first apply reports one hit');
t4.addNode({ localName: 'div', classes: ['extra'] });
var t4hits = t4.applyMore([action([cssSelector('div.extra')])]);
check(t4hits === 2,
    'persistent filterer: second apply reuses the same filterer (2 hits)');
var t4hidden = t4.hidden;
var t4again = t4.applyMore([action([cssSelector('div.extra')])]);
check(t4again === 2 && t4.hidden === t4hidden,
    'persistent filterer: duplicate actions deduped by raw selector');

var t5 = run([action([cssSelector('head')])], { nodes: [] });
var t5node = t5.addNode({ localName: 'head' });
t5.fireMutation([t5node]);
check(t5.hidden === 0,
    'domWatcher: head insertions are ignored (ignoreTags)');

// --- module load safety -----------------------------------------------------

var loadOk = false;
try {
    delete require.cache[require.resolve(ENGINE)];
    delete global.window;
    require(ENGINE);
    loadOk = true;
} catch (ex) {
    loadOk = false;
}
check(loadOk, 'engine loads without throwing when window is undefined');

console.log('');
if (failures === 0) {
    console.log('ALL PASS (' + passed + ' checks)');
} else {
    console.error(failures + ' FAILURE(S), ' + passed + ' passed');
    process.exitCode = 1;
}
