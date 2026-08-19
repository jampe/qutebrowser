// Procedural cosmetic filter engine for qutebrowser
// Adapted from uBlock Origin's contentscript-extra.js
// SPDX-License-Identifier: GPL-3.0-or-later
//
// This file is injected into the page's main world at DocumentReady.
// It exposes window._qutebrowser.proceduralFilterer.apply(actionsJSON)
// to execute procedural cosmetic filters (e.g. :upward(), :has(), :xpath()).

(function() {
    'use strict';

    // --- vAPI stub -----------------------------------------------------------
    // Minimal shim so PSelector classes work without the full uBO runtime.

    var vAPI = {
        randomToken: function() {
            return 'v' + Math.random().toString(36).slice(2, 10);
        },
        hideStyle: 'display:none!important;',
        domWatcher: {
            addListener: function() {}
        },
        domFilterer: {
            _styleElements: [],
            commit: function(callback) {
                if (callback) { callback(); }
                // Re-apply on demand (matches-media/watch-attr route through
                // onDOMChanged()). apply() sets proceduralFilterer BEFORE
                // addProceduralSelectors(), so the initial commit runs here
                // too - apply() must not call commitNow() again.
                if (this.proceduralFilterer instanceof Object) {
                    this.proceduralFilterer.commitNow();
                }
            },
            addCSS: function(rule, _options) {
                var head = document.head || document.documentElement;
                if (!head) { return; }
                var style = document.createElement('style');
                style.type = 'text/css';
                style.textContent = rule;
                head.appendChild(style);
                this._styleElements.push(style);
            },
            hasListeners: function() {
                return false;
            },
            triggerListeners: function() {}
        },
        DOMProceduralFilterer: null
    };

    // --- Helpers -------------------------------------------------------------

    var nonVisualElements = {
        head: true,
        link: true,
        meta: true,
        script: true,
        style: true,
    };

    var regexFromString = function(s, exact) {
        if (exact === undefined) { exact = false; }
        if (s === '') { return /^/; }
        var match = /^\/(.+)\/([imu]*)$/.exec(s);
        if (match !== null) {
            return new RegExp(match[1], match[2] || undefined);
        }
        var reStr = s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp(exact ? '^' + reStr + '$' : reStr);
    };

    // --- PSelector base classes ----------------------------------------------

    function PSelectorTask() {}
    PSelectorTask.prototype.begin = function() {};
    PSelectorTask.prototype.end = function() {};

    function PSelectorVoidTask(task) {
        PSelectorTask.call(this);
        console.info('qutebrowser: :' + task[0] + '() operator does not exist');
    }
    PSelectorVoidTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorVoidTask.prototype.constructor = PSelectorVoidTask;
    PSelectorVoidTask.prototype.transpose = function() {};

    // --- Operator task classes (all 17) --------------------------------------

    // 1. has-text
    function PSelectorHasTextTask(task) {
        PSelectorTask.call(this);
        this.needle = regexFromString(task[1]);
    }
    PSelectorHasTextTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorHasTextTask.prototype.constructor = PSelectorHasTextTask;
    PSelectorHasTextTask.prototype.transpose = function(node, output) {
        if (this.needle.test(node.textContent)) {
            output.push(node);
        }
    };

    // 2. if / has
    function PSelectorIfTask(task) {
        PSelectorTask.call(this);
        this.pselector = new PSelector(task[1]);
        this.target = true;
    }
    PSelectorIfTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorIfTask.prototype.constructor = PSelectorIfTask;
    PSelectorIfTask.prototype.transpose = function(node, output) {
        if (this.pselector.test(node) === this.target) {
            output.push(node);
        }
    };

    // 3. if-not / not
    function PSelectorIfNotTask(task) {
        PSelectorIfTask.call(this, task);
        this.target = false;
    }
    PSelectorIfNotTask.prototype = Object.create(PSelectorIfTask.prototype);
    PSelectorIfNotTask.prototype.constructor = PSelectorIfNotTask;

    // 4. matches-attr
    function PSelectorMatchesAttrTask(task) {
        PSelectorTask.call(this);
        this.reAttr = regexFromString(task[1].attr, true);
        this.reValue = regexFromString(task[1].value, true);
    }
    PSelectorMatchesAttrTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMatchesAttrTask.prototype.constructor = PSelectorMatchesAttrTask;
    PSelectorMatchesAttrTask.prototype.transpose = function(node, output) {
        if (typeof node.getAttributeNames !== 'function') { return; }
        var attrs = node.getAttributeNames();
        for (var i = 0; i < attrs.length; i++) {
            var attr = attrs[i];
            if (this.reAttr.test(attr) === false) { continue; }
            if (this.reValue.test(node.getAttribute(attr)) === false) { continue; }
            output.push(node);
        }
    };

    // 5. matches-css
    function PSelectorMatchesCSSTask(task) {
        PSelectorTask.call(this);
        this.name = task[1].name;
        this.pseudo = task[1].pseudo ? '::' + task[1].pseudo : null;
        var arg0 = task[1].value, arg1;
        if (Array.isArray(arg0)) {
            arg1 = arg0[1]; arg0 = arg0[0];
        }
        this.value = new RegExp(arg0, arg1);
    }
    PSelectorMatchesCSSTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMatchesCSSTask.prototype.constructor = PSelectorMatchesCSSTask;
    PSelectorMatchesCSSTask.prototype.transpose = function(node, output) {
        var style = window.getComputedStyle(node, this.pseudo);
        if (style !== null && this.value.test(style[this.name])) {
            output.push(node);
        }
    };

    // 6. matches-css-after
    function PSelectorMatchesCSSAfterTask(task) {
        PSelectorMatchesCSSTask.call(this, task);
        this.pseudo = '::after';
    }
    PSelectorMatchesCSSAfterTask.prototype = Object.create(PSelectorMatchesCSSTask.prototype);
    PSelectorMatchesCSSAfterTask.prototype.constructor = PSelectorMatchesCSSAfterTask;

    // 7. matches-css-before
    function PSelectorMatchesCSSBeforeTask(task) {
        PSelectorMatchesCSSTask.call(this, task);
        this.pseudo = '::before';
    }
    PSelectorMatchesCSSBeforeTask.prototype = Object.create(PSelectorMatchesCSSTask.prototype);
    PSelectorMatchesCSSBeforeTask.prototype.constructor = PSelectorMatchesCSSBeforeTask;

    // 8. matches-media
    function PSelectorMatchesMediaTask(task) {
        PSelectorTask.call(this);
        this.mql = window.matchMedia(task[1]);
        if (this.mql.media === 'not all') { return; }
        this.mql.addEventListener('change', function() {
            if (typeof vAPI !== 'object') { return; }
            if (vAPI === null) { return; }
            var filterer = vAPI.domFilterer && vAPI.domFilterer.proceduralFilterer;
            if (filterer instanceof Object === false) { return; }
            filterer.onDOMChanged([null]);
        });
    }
    PSelectorMatchesMediaTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMatchesMediaTask.prototype.constructor = PSelectorMatchesMediaTask;
    PSelectorMatchesMediaTask.prototype.transpose = function(node, output) {
        if (this.mql.matches === false) { return; }
        output.push(node);
    };

    // 9. matches-path
    function PSelectorMatchesPathTask(task) {
        PSelectorTask.call(this);
        this.needle = regexFromString(
            task[1].replace(/\P{ASCII}/gu, function(s) { return encodeURIComponent(s); })
        );
    }
    PSelectorMatchesPathTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMatchesPathTask.prototype.constructor = PSelectorMatchesPathTask;
    PSelectorMatchesPathTask.prototype.transpose = function(node, output) {
        if (this.needle.test(self.location.pathname + self.location.search)) {
            output.push(node);
        }
    };

    // 10. matches-prop
    function PSelectorMatchesPropTask(task) {
        PSelectorTask.call(this);
        this.props = task[1].attr.split('.');
        this.reValue = task[1].value !== ''
            ? regexFromString(task[1].value, true)
            : null;
    }
    PSelectorMatchesPropTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMatchesPropTask.prototype.constructor = PSelectorMatchesPropTask;
    PSelectorMatchesPropTask.prototype.transpose = function(node, output) {
        var value = node;
        for (var i = 0; i < this.props.length; i++) {
            var prop = this.props[i];
            if (value === undefined) { return; }
            if (value === null) { return; }
            value = value[prop];
        }
        if (this.reValue === null) {
            if (value === undefined) { return; }
        } else if (this.reValue.test(value) === false) {
            return;
        }
        output.push(node);
    };

    // 11. min-text-length
    function PSelectorMinTextLengthTask(task) {
        PSelectorTask.call(this);
        this.min = task[1];
    }
    PSelectorMinTextLengthTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorMinTextLengthTask.prototype.constructor = PSelectorMinTextLengthTask;
    PSelectorMinTextLengthTask.prototype.transpose = function(node, output) {
        if (node.textContent.length >= this.min) {
            output.push(node);
        }
    };

    // 12. others
    function PSelectorOthersTask() {
        PSelectorTask.call(this);
        this.targets = new Set();
    }
    PSelectorOthersTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorOthersTask.prototype.constructor = PSelectorOthersTask;
    PSelectorOthersTask.prototype.begin = function() {
        this.targets.clear();
    };
    PSelectorOthersTask.prototype.end = function(output) {
        var toKeep = new Set(this.targets);
        var toDiscard = new Set();
        var body = document.body;
        var head = document.head;
        var discard = null;
        var keep;
        for (keep of this.targets) {
            while (keep !== null && keep !== body && keep !== head) {
                toKeep.add(keep);
                toDiscard.delete(keep);
                discard = keep.previousElementSibling;
                while (discard !== null) {
                    if (nonVisualElements[discard.localName] !== true) {
                        if (toKeep.has(discard) === false) {
                            toDiscard.add(discard);
                        }
                    }
                    discard = discard.previousElementSibling;
                }
                discard = keep.nextElementSibling;
                while (discard !== null) {
                    if (nonVisualElements[discard.localName] !== true) {
                        if (toKeep.has(discard) === false) {
                            toDiscard.add(discard);
                        }
                    }
                    discard = discard.nextElementSibling;
                }
                keep = keep.parentElement;
            }
        }
        for (discard of toDiscard) {
            output.push(discard);
        }
        this.targets.clear();
    };
    PSelectorOthersTask.prototype.transpose = function(candidate) {
        for (var target of this.targets) {
            if (target.contains(candidate)) { return; }
            if (candidate.contains(target)) {
                this.targets.delete(target);
            }
        }
        this.targets.add(candidate);
    };

    // 13. shadow
    function PSelectorShadowTask(task) {
        PSelectorTask.call(this);
        this.selector = task[1];
    }
    PSelectorShadowTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorShadowTask.prototype.constructor = PSelectorShadowTask;
    PSelectorShadowTask.prototype.transpose = function(node, output) {
        var root = this.openOrClosedShadowRoot(node);
        if (root === null) { return; }
        var nodes = root.querySelectorAll(this.selector);
        output.push.apply(output, nodes);
    };
    PSelectorShadowTask.prototype.getOpenOrClosedShadowRoot = function() {
        if (PSelectorShadowTask._openOrClosedShadowRoot !== undefined) {
            return PSelectorShadowTask._openOrClosedShadowRoot;
        }
        if (typeof chrome === 'object' && chrome !== null) {
            if (chrome.dom instanceof Object) {
                if (typeof chrome.dom.openOrClosedShadowRoot === 'function') {
                    PSelectorShadowTask._openOrClosedShadowRoot =
                        chrome.dom.openOrClosedShadowRoot;
                    return PSelectorShadowTask._openOrClosedShadowRoot;
                }
            }
        }
        PSelectorShadowTask._openOrClosedShadowRoot = function(node) {
            return node.openOrClosedShadowRoot || null;
        };
        return PSelectorShadowTask._openOrClosedShadowRoot;
    };
    // Use a getter-compatible approach for the shadow root accessor
    Object.defineProperty(PSelectorShadowTask.prototype, 'openOrClosedShadowRoot', {
        get: function() {
            return this.getOpenOrClosedShadowRoot();
        }
    });

    // 14. spath
    function PSelectorSpathTask(task) {
        PSelectorTask.call(this);
        this.spath = task[1];
        this.nth = /^(?:\s*[+~]|:)/.test(this.spath);
        if (this.nth) { return; }
        if (/^\s*>/.test(this.spath)) {
            this.spath = ':scope ' + this.spath.trim();
        }
    }
    PSelectorSpathTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorSpathTask.prototype.constructor = PSelectorSpathTask;
    PSelectorSpathTask.prototype.transpose = function(node, output) {
        var nodes = this.nth
            ? PSelectorSpathTask.qsa(node, this.spath)
            : node.querySelectorAll(this.spath);
        for (var i = 0; i < nodes.length; i++) {
            output.push(nodes[i]);
        }
    };
    PSelectorSpathTask.qsa = function(node, selector) {
        var parent = node.parentElement;
        if (parent === null) { return []; }
        var pos = 1;
        var sibling = node;
        for (;;) {
            sibling = sibling.previousElementSibling;
            if (sibling === null) { break; }
            pos += 1;
        }
        return parent.querySelectorAll(
            ':scope > :nth-child(' + pos + ')' + selector
        );
    };

    // 15. upward
    function PSelectorUpwardTask(task) {
        PSelectorTask.call(this);
        var arg = task[1];
        if (typeof arg === 'number') {
            this.i = arg;
        } else {
            this.s = arg;
        }
    }
    PSelectorUpwardTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorUpwardTask.prototype.constructor = PSelectorUpwardTask;
    PSelectorUpwardTask.prototype.i = 0;
    PSelectorUpwardTask.prototype.s = '';
    PSelectorUpwardTask.prototype.transpose = function(node, output) {
        if (this.s !== '') {
            var parent = node.parentElement;
            if (parent === null) { return; }
            node = parent.closest(this.s);
            if (node === null) { return; }
        } else {
            var nth = this.i;
            for (;;) {
                node = node.parentElement;
                if (node === null) { return; }
                nth -= 1;
                if (nth === 0) { break; }
            }
        }
        output.push(node);
    };

    // 16. watch-attr
    function PSelectorWatchAttrs(task) {
        PSelectorTask.call(this);
        this.observer = null;
        this.observed = new WeakSet();
        this.observerOptions = {
            attributes: true,
            subtree: true,
        };
        var attrs = task[1];
        if (Array.isArray(attrs) && attrs.length !== 0) {
            this.observerOptions.attributeFilter = task[1];
        }
    }
    PSelectorWatchAttrs.prototype = Object.create(PSelectorTask.prototype);
    PSelectorWatchAttrs.prototype.constructor = PSelectorWatchAttrs;
    PSelectorWatchAttrs.prototype.handler = function() {
        var filterer =
            vAPI.domFilterer && vAPI.domFilterer.proceduralFilterer;
        if (filterer instanceof Object) {
            filterer.onDOMChanged([null]);
        }
    };
    PSelectorWatchAttrs.prototype.transpose = function(node, output) {
        output.push(node);
        if (this.observed.has(node)) { return; }
        if (this.observer === null) {
            var self = this;
            this.observer = new MutationObserver(function() {
                self.handler();
            });
        }
        this.observer.observe(node, this.observerOptions);
        this.observed.add(node);
    };

    // 17. xpath
    function PSelectorXpathTask(task) {
        PSelectorTask.call(this);
        this.xpe = document.createExpression(task[1], null);
        this.xpr = null;
    }
    PSelectorXpathTask.prototype = Object.create(PSelectorTask.prototype);
    PSelectorXpathTask.prototype.constructor = PSelectorXpathTask;
    PSelectorXpathTask.prototype.transpose = function(node, output) {
        this.xpr = this.xpe.evaluate(
            node,
            XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE,
            this.xpr
        );
        var j = this.xpr.snapshotLength;
        while (j--) {
            var snapNode = this.xpr.snapshotItem(j);
            if (snapNode.nodeType === 1) {
                output.push(snapNode);
            }
        }
    };

    // --- PSelector class -----------------------------------------------------

    function PSelector(o) {
        this.selector = o.selector;
        this.tasks = [];
        var tasks = [];
        if (Array.isArray(o.tasks) === false) { return; }
        for (var i = 0; i < o.tasks.length; i++) {
            var task = o.tasks[i];
            var ctor = PSelector.prototype.operatorToTaskMap.get(task[0]) || PSelectorVoidTask;
            tasks.push(new ctor(task));
        }
        this.tasks = tasks;
    }

    PSelector.prototype.operatorToTaskMap = new Map([
        ['has', PSelectorIfTask],
        ['has-text', PSelectorHasTextTask],
        ['if', PSelectorIfTask],
        ['if-not', PSelectorIfNotTask],
        ['matches-attr', PSelectorMatchesAttrTask],
        ['matches-css', PSelectorMatchesCSSTask],
        ['matches-css-after', PSelectorMatchesCSSAfterTask],
        ['matches-css-before', PSelectorMatchesCSSBeforeTask],
        ['matches-media', PSelectorMatchesMediaTask],
        ['matches-path', PSelectorMatchesPathTask],
        ['matches-prop', PSelectorMatchesPropTask],
        ['min-text-length', PSelectorMinTextLengthTask],
        ['not', PSelectorIfNotTask],
        ['others', PSelectorOthersTask],
        ['shadow', PSelectorShadowTask],
        ['spath', PSelectorSpathTask],
        ['upward', PSelectorUpwardTask],
        ['watch-attr', PSelectorWatchAttrs],
        ['xpath', PSelectorXpathTask],
    ]);

    PSelector.prototype.prime = function(input) {
        var root = input || document;
        if (this.selector === '') { return [root]; }
        if (input !== document) {
            var c0 = this.selector.charCodeAt(0);
            if (c0 === 0x2B /* + */ || c0 === 0x7E /* ~ */) {
                return Array.from(PSelectorSpathTask.qsa(input, this.selector));
            } else if (c0 === 0x3E /* > */) {
                return Array.from(input.querySelectorAll(':scope ' + this.selector));
            }
        }
        return Array.from(root.querySelectorAll(this.selector));
    };

    PSelector.prototype.exec = function(input) {
        var nodes = this.prime(input);
        for (var t = 0; t < this.tasks.length; t++) {
            var task = this.tasks[t];
            if (nodes.length === 0) { break; }
            var transposed = [];
            task.begin();
            for (var n = 0; n < nodes.length; n++) {
                task.transpose(nodes[n], transposed);
            }
            task.end(transposed);
            nodes = transposed;
        }
        return nodes;
    };

    PSelector.prototype.test = function(input) {
        var nodes = this.prime(input);
        for (var i = 0; i < nodes.length; i++) {
            var output = [nodes[i]];
            for (var t = 0; t < this.tasks.length; t++) {
                var task = this.tasks[t];
                var transposed = [];
                task.begin();
                for (var n = 0; n < output.length; n++) {
                    task.transpose(output[n], transposed);
                }
                task.end(transposed);
                output = transposed;
                if (output.length === 0) { break; }
            }
            if (output.length !== 0) { return true; }
        }
        return false;
    };

    // --- PSelectorRoot class -------------------------------------------------

    function PSelectorRoot(o) {
        PSelector.call(this, o);
        this.budget = 200;
        this.raw = o.raw;
        this.cost = 0;
        this.lastAllowanceTime = 0;
        this.action = o.action;
    }
    PSelectorRoot.prototype = Object.create(PSelector.prototype);
    PSelectorRoot.prototype.constructor = PSelectorRoot;
    PSelectorRoot.prototype.hit = false;
    PSelectorRoot.prototype.prime = function(input) {
        try {
            return PSelector.prototype.prime.call(this, input);
        } catch (e) {
            console.warn(
                'qutebrowser: invalid procedural selector', this.raw, e);
        }
        return [];
    };

    // --- ProceduralFilterer class --------------------------------------------

    function ProceduralFilterer(domFilterer) {
        this.domFilterer = domFilterer;
        this.mustApplySelectors = false;
        this.selectors = new Map();
        this.masterToken = vAPI.randomToken();
        this.styleTokenMap = new Map();
        this.styledNodes = new Set();
        if (vAPI.domWatcher instanceof Object) {
            vAPI.domWatcher.addListener(this);
        }
    }

    ProceduralFilterer.prototype.addProceduralSelectors = function(selectors) {
        var addedSelectors = [];
        var mustCommit = false;
        for (var i = 0; i < selectors.length; i++) {
            var selector = selectors[i];
            if (this.selectors.has(selector.raw)) { continue; }
            // Per-filter error isolation for CONSTRUCTION: task constructors
            // can throw on filter-controlled input (xpath/regex), which would
            // otherwise abort the whole batch.
            var pselector;
            try {
                pselector = new PSelectorRoot(selector);
                this.primeProceduralSelector(pselector);
            } catch (ex) {
                console.warn(
                    'qutebrowser: failed to create procedural filter',
                    selector.raw, ex
                );
                continue;
            }
            this.selectors.set(selector.raw, pselector);
            addedSelectors.push(pselector);
            mustCommit = true;
        }
        if (mustCommit === false) { return; }
        this.mustApplySelectors = this.selectors.size !== 0;
        this.domFilterer.commit();
        if (this.domFilterer.hasListeners()) {
            this.domFilterer.triggerListeners({
                procedural: addedSelectors
            });
        }
    };

    ProceduralFilterer.prototype.primeProceduralSelector = function(pselector) {
        if (pselector.action === undefined) {
            this.styleTokenFromStyle(vAPI.hideStyle);
        } else if (pselector.action[0] === 'style') {
            this.styleTokenFromStyle(pselector.action[1]);
        }
        return pselector;
    };

    ProceduralFilterer.prototype.commitNow = function() {
        if (this.selectors.size === 0) { return; }

        this.mustApplySelectors = false;

        var toUnstyle = this.styledNodes;
        this.styledNodes = new Set();

        var t0 = Date.now();

        var iter = this.selectors.values();
        var step = iter.next();
        while (!step.done) {
            var pselector = step.value;
            step = iter.next();

            // Per-filter error isolation: one bad filter cannot kill all filtering
            try {
                var allowance = Math.floor((t0 - pselector.lastAllowanceTime) / 2000);
                if (allowance >= 1) {
                    pselector.budget += allowance * 50;
                    if (pselector.budget > 200) { pselector.budget = 200; }
                    pselector.lastAllowanceTime = t0;
                }
                if (pselector.budget <= 0) { continue; }
                var nodes = pselector.exec();
                var t1 = Date.now();
                pselector.budget += t0 - t1;
                if (pselector.budget < -500) {
                    console.info('qutebrowser: disabling %s', pselector.raw);
                    pselector.budget = -0x7FFFFFFF;
                }
                t0 = t1;
                if (nodes.length === 0) { continue; }
                pselector.hit = true;
                this.processNodes(nodes, pselector.action);
            } catch (ex) {
                console.warn('qutebrowser: procedural filter error for', pselector.raw, ex);
            }
        }

        this.unprocessNodes(toUnstyle);
    };

    ProceduralFilterer.prototype.styleTokenFromStyle = function(style) {
        if (style === undefined) { return; }
        var styleToken = this.styleTokenMap.get(style);
        if (styleToken !== undefined) { return styleToken; }
        styleToken = vAPI.randomToken();
        this.styleTokenMap.set(style, styleToken);
        this.domFilterer.addCSS(
            '[' + this.masterToken + '][' + styleToken + ']\n{' + style + '}',
            { silent: true, mustInject: true }
        );
        return styleToken;
    };

    ProceduralFilterer.prototype.processNodes = function(nodes, action) {
        var op = action && action[0] || '';
        var arg = op !== '' ? action[1] : '';
        switch (op) {
        case '':
            /* fall through */
        case 'style': {
            var styleToken = this.styleTokenFromStyle(
                arg === '' ? vAPI.hideStyle : arg
            );
            for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                node.setAttribute(this.masterToken, '');
                node.setAttribute(styleToken, '');
                this.styledNodes.add(node);
            }
            break;
        }
        case 'remove': {
            for (var i = 0; i < nodes.length; i++) {
                nodes[i].remove();
                nodes[i].textContent = '';
            }
            break;
        }
        case 'remove-attr': {
            var reAttr = regexFromString(arg, true);
            for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                var names = node.getAttributeNames();
                for (var j = 0; j < names.length; j++) {
                    if (reAttr.test(names[j]) === false) { continue; }
                    node.removeAttribute(names[j]);
                }
            }
            break;
        }
        case 'remove-class': {
            var reClass = regexFromString(arg, true);
            for (var i = 0; i < nodes.length; i++) {
                var cl = nodes[i].classList;
                var cls = Array.from(cl.values());
                for (var j = 0; j < cls.length; j++) {
                    if (reClass.test(cls[j]) === false) { continue; }
                    cl.remove(cls[j]);
                }
            }
            break;
        }
        default:
            break;
        }
    };

    ProceduralFilterer.prototype.unprocessNodes = function(nodes) {
        for (var node of nodes) {
            if (this.styledNodes.has(node)) { continue; }
            node.removeAttribute(this.masterToken);
        }
    };

    ProceduralFilterer.prototype.createProceduralFilter = function(o) {
        return this.primeProceduralSelector(
            new PSelectorRoot(typeof o === 'string' ? JSON.parse(o) : o)
        );
    };

    ProceduralFilterer.prototype.onDOMCreated = function() {};

    ProceduralFilterer.prototype.onDOMChanged = function(addedNodes, removedNodes) {
        if (this.selectors.size === 0) { return; }
        this.mustApplySelectors =
            this.mustApplySelectors ||
            addedNodes.length !== 0 ||
            removedNodes;
        this.domFilterer.commit();
    };

    // Register on vAPI for operator callbacks (matches-media, watch-attr)
    vAPI.domFilterer.proceduralFilterer = null; // set after instantiation

    // --- Translation layer: adblock-rust JSON -> PSelector format ------------

    function translateAdblockActions(actionsJSON) {
        // actionsJSON is an array of JSON strings from the procedural_actions Set
        var results = [];
        for (var i = 0; i < actionsJSON.length; i++) {
            try {
                var raw = actionsJSON[i];
                var parsed = JSON.parse(raw);

                // Find the first css-selector in the selector array
                var cssSelector = '';
                var tasks = [];
                var selectors = parsed.selector || [];

                for (var j = 0; j < selectors.length; j++) {
                    var sel = selectors[j];
                    if (sel.type === 'css-selector') {
                        if (cssSelector === '') {
                            cssSelector = sel.arg;
                        }
                        // Additional css-selectors after the first become spath tasks
                        else {
                            tasks.push(['spath', sel.arg]);
                        }
                    } else {
                        tasks.push(convertOperator(sel.type, sel.arg));
                    }
                }

                // Convert action
                var action = ['', ''];
                if (parsed.action) {
                    action = convertAction(parsed.action);
                }

                results.push({
                    selector: cssSelector,
                    tasks: tasks,
                    action: action,
                    raw: raw
                });
            } catch (ex) {
                console.warn('qutebrowser: failed to translate procedural action', ex);
            }
        }
        return results;
    }

    function convertOperator(type, arg) {
        switch (type) {
        case 'upward':
            // If arg is a numeric string, convert to Number for PSelectorUpwardTask
            if (/^\d+$/.test(arg)) {
                return ['upward', Number(arg)];
            }
            return ['upward', arg];

        case 'matches-attr':
            // adblock-rust sends "attr=value" as flat string
            // Split on first = to get {attr, value} object
            var eqPos = arg.indexOf('=');
            if (eqPos === -1) {
                return ['matches-attr', { attr: arg, value: '' }];
            }
            return ['matches-attr', {
                attr: arg.substring(0, eqPos),
                value: arg.substring(eqPos + 1)
            }];

        case 'matches-css':
        case 'matches-css-before':
        case 'matches-css-after':
            // adblock-rust sends "name:value" as flat string (CSS tokenizer
            // round-trip). Split on first : to get {name, pseudo, value}.
            // Match uBO compileCSSDeclaration semantics:
            //  - regex values arrive WITH wrapping slashes (/re/flags); strip
            //    them so new RegExp(source) tests the computed style, not the
            //    literal "/re/" text
            //  - plain values must be anchored AND escaped: /^escaped$/, so
            //    "10px" does not match a computed "110px"
            var colonPos = arg.indexOf(':');
            var cssName = colonPos === -1
                ? arg.trim()
                : arg.substring(0, colonPos).trim();
            var cssValue = colonPos === -1
                ? ''
                : arg.substring(colonPos + 1).trim();
            var reLiteral = /^\/(.+)\/([imu]*)$/.exec(cssValue);
            if (reLiteral !== null) {
                if (reLiteral[2] !== '') {
                    return [type, {
                        name: cssName,
                        pseudo: null,
                        value: [reLiteral[1], reLiteral[2]]
                    }];
                }
                return [type, {
                    name: cssName,
                    pseudo: null,
                    value: reLiteral[1]
                }];
            }
            return [type, {
                name: cssName,
                pseudo: null,
                value: '^' + cssValue.replace(
                    /[.*+?^${}()|[\]\\]/g, '\\$&') + '$'
            }];

        // These operators pass arg string directly
        case 'has-text':
        case 'has':
        case 'if':
        case 'if-not':
        case 'not':
        case 'matches-path':
        case 'min-text-length':
        case 'xpath':
        case 'matches-media':
        case 'matches-prop':
        case 'shadow':
        case 'spath':
        case 'others':
        case 'watch-attr':
            return [type, arg];

        default:
            return [type, arg];
        }
    }

    function convertAction(action) {
        switch (action.type) {
        case 'remove':
            return ['remove', ''];
        case 'style':
            return ['style', action.arg];
        case 'remove-attr':
            return ['remove-attr', action.arg];
        case 'remove-class':
            return ['remove-class', action.arg];
        default:
            return ['', ''];
        }
    }

    // --- Public API ----------------------------------------------------------
    // Guard so the module also loads cleanly under Node; export only in a browser.

    if (typeof window !== 'undefined') {
        window._qutebrowser = window._qutebrowser || {};

        window._qutebrowser.proceduralFilterer = {
            apply: function(actionsJSON) {
                try {
                    var translated = translateAdblockActions(actionsJSON);
                    if (translated.length === 0) { return 0; }

                    var filterer = new ProceduralFilterer(vAPI.domFilterer);
                    vAPI.domFilterer.proceduralFilterer = filterer;

                    filterer.addProceduralSelectors(translated);

                    // addProceduralSelectors() calls domFilterer.commit(),
                    // which runs commitNow() via the stub wiring; calling
                    // commitNow() again here would evaluate every filter
                    // twice (and consume budget twice).

                    // Count selectors that matched nodes (hit set in commitNow())
                    var hitCount = 0;
                    var iter = filterer.selectors.values();
                    var step = iter.next();
                    while (!step.done) {
                        if (step.value.hit === true) {
                            hitCount++;
                        }
                        step = iter.next();
                    }
                    return hitCount;
                } catch (ex) {
                    console.warn('qutebrowser: procedural filter engine error', ex);
                    return 0;
                }
            }
        };
    }

})();
