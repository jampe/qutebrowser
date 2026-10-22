// Continuous generic cosmetic-filter surveyor for qutebrowser
// Adapted from uBlock Origin's vAPI.domSurveyor
// (uBlock/src/js/contentscript.js:924-1194)
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Injected into the page's main world once per document after load_finished.
// Collects individual class/id tokens added to the DOM; qutebrowser polls
// window._qutebrowser.domSurveyor.drain() and queries the adblock engine for
// matching generic hide rules.

(function() {
    'use strict';

    if (typeof window === 'undefined' || typeof document === 'undefined') {
        return;
    }
    if (window._qutebrowser && window._qutebrowser.domSurveyor) {
        return;  // already installed for this document
    }

    var maxSurveyNodes = 65536;
    var chunkSize = 256;
    var ignoreTags = {
        br: true, head: true, link: true, meta: true, script: true, style: true
    };

    var surveyedClasses = new Set();
    var surveyedIds = new Set();
    var pendingClasses = new Set();
    var pendingIds = new Set();
    var visited = new WeakSet();
    var queue = [];
    var scannedNodes = 0;
    var scheduled = false;
    var stopped = false;
    var observer;

    var addClassToken = function(token) {
        token = token.trim();
        if (token.length === 0) { return; }
        if (surveyedClasses.has(token) || pendingClasses.has(token)) { return; }
        pendingClasses.add(token);
    };

    var addId = function(token) {
        token = token.trim();
        if (token.length === 0) { return; }
        if (surveyedIds.has(token) || pendingIds.has(token)) { return; }
        pendingIds.add(token);
    };

    var surveyNode = function(node, force) {
        if (stopped) { return false; }
        if (node.nodeType !== 1) { return false; }
        if (force !== true && visited.has(node)) { return false; }
        if (ignoreTags[node.localName] === true) { return false; }
        // uBO queries html/body tokens with 'safeOnly'; the engine API has no
        // safe mode, so skip them (avoids hiding the whole page).
        if (node === document.documentElement || node === document.body) { return false; }
        // Do not mark transiently detached nodes as visited: they may be
        // re-attached later (virtualized lists, reparenting) and must be
        // surveyed again.
        if (node.parentElement === null) { return false; }
        if (force !== true) { visited.add(node); }
        scannedNodes += 1;
        if (scannedNodes >= maxSurveyNodes) {
            stop();
        }
        var id = node.id;
        if (typeof id === 'string' && id.length !== 0) {
            addId(id);
        }
        var cls = node.getAttribute('class');
        if (typeof cls === 'string' && cls.length !== 0) {
            var tokens = cls.split(/\s+/);
            for (var i = 0; i < tokens.length; i++) {
                addClassToken(tokens[i]);
            }
        }
        return true;
    };

    var enqueueSubtree = function(root) {
        var nodes = root.querySelectorAll('[id],[class]');
        for (var i = 0; i < nodes.length; i++) {
            queue.push(nodes[i]);
        }
    };

    var processQueue = function() {
        scheduled = false;
        var end = Math.min(queue.length, chunkSize);
        for (var i = 0; i < end; i++) {
            var node = queue[i];
            // Only enqueue descendants for a node surveyed for the first time;
            // otherwise a deep chain re-runs querySelectorAll once per ancestor.
            if (surveyNode(node) && node.firstElementChild !== null) {
                enqueueSubtree(node);
            }
        }
        queue.splice(0, end);
        if (queue.length !== 0 && !stopped) {
            schedule();
        }
    };

    var schedule = function() {
        if (scheduled || stopped) { return; }
        scheduled = true;
        setTimeout(processQueue, 0);
    };

    var onMutations = function(mutations) {
        if (stopped) { return; }
        for (var i = 0; i < mutations.length; i++) {
            var mutation = mutations[i];
            if (mutation.type === 'attributes') {
                surveyNode(mutation.target, true);
                continue;
            }
            var added = mutation.addedNodes;
            for (var j = 0; j < added.length; j++) {
                if (added[j].nodeType === 1) {
                    queue.push(added[j]);
                }
            }
        }
        if (queue.length !== 0) {
            schedule();
        }
    };

    var initialScan = function() {
        enqueueSubtree(document);
        if (queue.length !== 0) {
            schedule();
        }
    };

    var drain = function() {
        var result = {
            classes: Array.from(pendingClasses),
            ids: Array.from(pendingIds)
        };
        pendingClasses.forEach(function(token) { surveyedClasses.add(token); });
        pendingIds.forEach(function(token) { surveyedIds.add(token); });
        pendingClasses.clear();
        pendingIds.clear();
        return result;
    };

    var stop = function() {
        stopped = true;
        queue.length = 0;
        if (observer !== undefined) {
            observer.disconnect();
            observer = undefined;
        }
    };

    window._qutebrowser = window._qutebrowser || {};
    window._qutebrowser.domSurveyor = { drain: drain };

    observer = new MutationObserver(onMutations);
    observer.observe(document, {
        attributeFilter: ['class', 'id'],
        attributes: true,
        childList: true,
        subtree: true
    });

    if (document.body !== null) {
        initialScan();
    } else if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialScan, { once: true });
    } else {
        initialScan();
    }
})();
