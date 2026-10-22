#!/usr/bin/env node
/*
 * Regression tests for qutebrowser/javascript/dom-surveyor.js
 *
 * Verifies the continuous generic class/id surveyor:
 *  - individual class tokens (not the joined className string)
 *  - dedupe and drain semantics
 *  - attribute changes (class/id)
 *  - childList insertions incl. descendants
 *  - ignored tags, detached nodes, html/body
 *  - detach-then-reattach is surveyed again
 *  - chunked processing and the maxSurveyNodes shutdown
 *  - idempotent installation and no-DOM safety
 *
 * Run: node tests/unit/javascript/test_dom_surveyor.js
 */
'use strict';

var path = require('path');
var ENGINE = path.join(__dirname, '..', '..', '..', 'qutebrowser',
    'javascript', 'dom-surveyor.js');

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

function makeNode(opts, registry) {
    opts = opts || {};
    var node = {
        nodeType: 1,
        localName: opts.localName || 'div',
        id: opts.id || '',
        className: opts.className || '',
        parentElement: null,
        firstElementChild: null,
        _children: [],
        _qsaCalls: 0,
        getAttribute: function(name) {
            if (name !== 'class') { return null; }
            return this.className === '' ? null : this.className;
        },
        appendChild: function(child) {
            child.parentElement = this;
            this._children.push(child);
            if (this.firstElementChild === null) { this.firstElementChild = child; }
            return child;
        },
        querySelectorAll: function(sel) {
            this._qsaCalls += 1;
            registry.qsaCalls += 1;
            return descendantsMatching(this, registry);
        },
    };
    registry.nodes.push(node);
    return node;
}

function descendantsMatching(root, registry) {
    var out = [];
    var walk = function(parent) {
        for (var i = 0; i < parent._children.length; i++) {
            var child = parent._children[i];
            if (child.id !== '' || child.className !== '') { out.push(child); }
            walk(child);
        }
    };
    walk(root);
    return out;
}

function isAttached(node, documentElement) {
    var cur = node;
    while (cur) {
        if (cur === documentElement) { return true; }
        cur = cur.parentElement;
    }
    return false;
}

function setup() {
    var registry = { nodes: [], qsaCalls: 0 };
    var html = makeNode({ localName: 'html' }, registry);
    var body = makeNode({ localName: 'body' }, registry);
    html.appendChild(body);
    html.parentElement = null;

    global.window = global;
    global.document = {
        readyState: 'complete',
        documentElement: html,
        body: body,
        addEventListener: function(name, fn) { global.__domContentLoaded = fn; },
        querySelectorAll: function(sel) {
            registry.qsaCalls += 1;
            return registry.nodes.filter(function(n) {
                return (n.id !== '' || n.className !== '') &&
                    isAttached(n, html);
            });
        },
    };
    global.MutationObserver = function(callback) {
        global.__mutationObserverCallback = callback;
        global.__observerDisconnected = false;
    };
    global.MutationObserver.prototype.observe = function() {};
    global.MutationObserver.prototype.disconnect = function() {
        global.__observerDisconnected = true;
    };
    setTimeout = function(fn) { fn(); return 0; };

    delete require.cache[require.resolve(ENGINE)];
    delete global.window._qutebrowser;
    return { registry: registry, html: html, body: body };
}

function loadSurveyor() {
    require(ENGINE);
    return window._qutebrowser.domSurveyor;
}

function drain() {
    return window._qutebrowser.domSurveyor.drain();
}

function fireChildList(added) {
    global.__mutationObserverCallback([{ type: 'childList', addedNodes: added }]);
}

function fireAttribute(target, name) {
    global.__mutationObserverCallback([{
        type: 'attributes', target: target, attributeName: name,
    }]);
}

// --- s1/s2: initial scan + token splitting --------------------------------

var dom = setup();
var ad1 = makeNode({ className: 'ad-banner extra' }, dom.registry);
var ad2 = makeNode({ id: 'ad-slot' }, dom.registry);
dom.body.appendChild(ad1);
dom.body.appendChild(ad2);
loadSurveyor();

var r1 = drain();
check(JSON.stringify(r1.classes) === JSON.stringify(['ad-banner', 'extra']) &&
    JSON.stringify(r1.ids) === JSON.stringify(['ad-slot']),
    'initial scan drains individual class tokens and ids');

var r1b = drain();
check(r1b.classes.length === 0 && r1b.ids.length === 0,
    'second drain is empty (tokens marked as surveyed)');

// --- s3: attribute changes -------------------------------------------------

ad1.className = 'ad-banner extra ad-new';
ad2.id = 'ad-id2';
fireAttribute(ad1, 'class');
fireAttribute(ad2, 'id');
var r3 = drain();
check(r3.classes.indexOf('ad-new') !== -1 && r3.classes.length === 1,
    'class attribute change adds only the new token');
check(r3.ids.indexOf('ad-id2') !== -1 && r3.ids.length === 1,
    'id attribute change adds only the new id');

// --- s4: childList insertion incl. descendants ----------------------------

var parent = makeNode({ className: 'ad-new2' }, dom.registry);
var child = makeNode({ className: 'ad-child' }, dom.registry);
parent.appendChild(child);
dom.body.appendChild(parent);
var qsaBefore = dom.registry.qsaCalls;
fireChildList([parent]);
var r4 = drain();
check(r4.classes.indexOf('ad-new2') !== -1 &&
    r4.classes.indexOf('ad-child') !== -1,
    'inserted node and its descendants are surveyed');
check(dom.registry.qsaCalls - qsaBefore <= 3,
    'subtree scan is linear (qsa calls bounded)');

// --- s5: ignored nodes -----------------------------------------------------

var scriptNode = makeNode({ localName: 'script', className: 'ad-script' },
    dom.registry);
var headNode = makeNode({ localName: 'head', className: 'ad-head' },
    dom.registry);
var detached = makeNode({ className: 'ad-detached' }, dom.registry);
dom.html.className = 'htmlclass';
dom.body.className = 'bodyclass';
fireChildList([scriptNode, headNode, detached]);
var r5 = drain();
check(r5.classes.indexOf('ad-script') === -1 &&
    r5.classes.indexOf('ad-head') === -1 &&
    r5.classes.indexOf('ad-detached') === -1 &&
    r5.classes.indexOf('htmlclass') === -1 &&
    r5.classes.indexOf('bodyclass') === -1,
    'script/head/detached/html/body tokens are ignored');

// --- s5b: detach-then-reattach --------------------------------------------

var moved = makeNode({ className: 'ad-reattach' }, dom.registry);
fireChildList([moved]);  // surveyed while detached: not marked visited
var r5b = drain();
check(r5b.classes.indexOf('ad-reattach') === -1,
    'detached node is not surveyed');
dom.body.appendChild(moved);
fireChildList([moved]);
var r5b2 = drain();
check(r5b2.classes.indexOf('ad-reattach') !== -1,
    'reattached node is surveyed again');

// --- s6: chunking ----------------------------------------------------------

var nodes = [];
for (var i = 0; i < 300; i++) {
    var chunkNode = makeNode({ className: 'chunk-' + i }, dom.registry);
    dom.body.appendChild(chunkNode);
    nodes.push(chunkNode);
}
fireChildList(nodes);
var r6 = drain();
check(r6.classes.length === 300 &&
    r6.classes.indexOf('chunk-0') !== -1 &&
    r6.classes.indexOf('chunk-299') !== -1,
    'more than one chunk of added nodes is fully surveyed');

// --- s7: maxSurveyNodes shutdown ------------------------------------------

var bulk = [];
for (var b = 0; b < 70000; b++) {
    bulk.push({
        nodeType: 1,
        localName: 'div',
        id: '',
        className: 'bulk-' + b,
        parentElement: dom.body,
        firstElementChild: null,
        getAttribute: function(name) {
            return name === 'class' ? this.className : null;
        },
        querySelectorAll: function() { return []; },
    });
}
fireChildList(bulk);
var r7 = drain();
check(global.__observerDisconnected === true && r7.classes.length > 60000,
    'maxSurveyNodes disconnects the observer and keeps pending tokens');

fireChildList([makeNode({ className: 'after-stop' }, dom.registry)]);
var r7b = drain();
check(r7b.classes.length === 0,
    'no tokens are collected after shutdown');

// --- s8: idempotent installation ------------------------------------------

var firstDrain = window._qutebrowser.domSurveyor.drain;
delete require.cache[require.resolve(ENGINE)];
require(ENGINE);
check(window._qutebrowser.domSurveyor.drain === firstDrain,
    'second installation is a no-op');

// --- s9: no DOM ------------------------------------------------------------

var noDomOk = false;
try {
    delete require.cache[require.resolve(ENGINE)];
    delete global.window;
    delete global.document;
    require(ENGINE);
    noDomOk = true;
} catch (ex) {
    noDomOk = false;
}
check(noDomOk, 'engine loads without throwing when window/document are absent');

console.log('');
if (failures === 0) {
    console.log('ALL PASS (' + passed + ' checks)');
} else {
    console.error(failures + ' FAILURE(S), ' + passed + ' passed');
    process.exitCode = 1;
}
