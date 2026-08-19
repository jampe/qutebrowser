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

function run(actions, env) {
    // env: { style, media, badXpath }
    // Counters must be live (getters) — listeners fired after apply() (e.g.
    // matches-media change) mutate them after this function has returned.
    var counters = { hidden: 0, removed: 0 };
    var node = {
        textContent: 'some ad text',
        parentElement: null,
        localName: 'div',
        getAttributeNames: function() { return []; },
        getAttribute: function() { return null; },
        setAttribute: function() { counters.hidden++; },
        removeAttribute: function() {},
        contains: function() { return false; },
        remove: function() { counters.removed++; },
        previousElementSibling: null,
        nextElementSibling: null,
    };
    var style = env.style || {};
    var media = env.media || {
        media: 'not all',
        matches: false,
        addEventListener: function(name, fn) { this.listener = fn; }
    };
    global.window = global;
    global.document = {
        head: null,
        body: null,
        documentElement: null,
        querySelectorAll: function(sel) {
            if (sel === 'div' || sel === '.ad') { return [node]; }
            return [];
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
    global.MutationObserver = function() {};
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
        media: media
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
