// SPDX-FileCopyrightText: qutebrowser contributors
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Bitwarden ApplicationWorld content script: field discovery, focus events,
// inline menu, fill, and SPA auto-fill requests. Never sends field values out.

(function () {
    "use strict";

    if (window.__quteBitwarden) {
        return;
    }

    var state = {
        activeField: null,
        menuHost: null,
        menuShadow: null,
        filled: false,
        route: window.location.href,
        requestId: 0
    };
    window.__quteBitwarden = state;

    function messaging() {
        return window._qutebrowser &&
            window._qutebrowser.extensions &&
            window._qutebrowser.extensions.messaging;
    }

    function send(message) {
        var api = messaging();
        if (!api) {
            return false;
        }
        var envelope = {channel: "bitwarden"};
        for (var key in message) {
            if (Object.prototype.hasOwnProperty.call(message, key)) {
                envelope[key] = message[key];
            }
        }
        api.send(envelope);
        return true;
    }

    // Field discovery -----------------------------------------------------

    function collectRoots(root, out) {
        var elements;
        try {
            elements = root.querySelectorAll(
                "input, textarea, select, [contenteditable]");
        } catch (e) {
            return;
        }
        for (var i = 0; i < elements.length; i++) {
            out.push(elements[i]);
        }
        var all;
        try {
            all = root.querySelectorAll("*");
        } catch (e) {
            return;
        }
        for (var j = 0; j < all.length; j++) {
            if (all[j].shadowRoot) {
                collectRoots(all[j].shadowRoot, out);
            }
        }
    }

    function isVisible(element) {
        if (!element || !element.isConnected) {
            return false;
        }
        var style = window.getComputedStyle(element);
        if (style.visibility === "hidden" || style.display === "none") {
            return false;
        }
        return element.getClientRects().length > 0;
    }

    function attr(element, name) {
        var value = element.getAttribute(name);
        return value ? value.toLowerCase() : "";
    }

    function classify(element) {
        if (!element || !isVisible(element) || element.disabled ||
                element.readOnly) {
            return null;
        }
        var tag = element.tagName.toLowerCase();
        var type = attr(element, "type");
        var autocomplete = attr(element, "autocomplete");
        var name = attr(element, "name");
        var id = attr(element, "id");
        var placeholder = attr(element, "placeholder");
        var text = [name, id, placeholder, autocomplete].join(" ");

        if (type === "password") {
            return "password";
        }
        if (autocomplete === "one-time-code" || /(^|\b)(otp|totp|2fa)(\b|$)/.test(text)) {
            return "totp";
        }
        if (tag === "input" && (type === "email" ||
                autocomplete === "username" || autocomplete === "email")) {
            return "username";
        }
        if (type === "text" || type === "email" || type === "tel" ||
                tag === "textarea" || element.isContentEditable) {
            if (/(user|login|email|account)/.test(text)) {
                return "username";
            }
        }
        return null;
    }

    function findFields() {
        var found = {username: null, password: null, totp: null};
        var elements = [];
        collectRoots(document, elements);
        for (var i = 0; i < elements.length; i++) {
            var kind = classify(elements[i]);
            if (kind && !found[kind]) {
                found[kind] = elements[i];
            }
        }
        return found;
    }

    function collectFields() {
        var fields = findFields();
        return {
            url: window.location.href,
            username: !!fields.username,
            password: !!fields.password,
            totp: !!fields.totp
        };
    }

    window.__bwCollectFields = collectFields;

    function setValue(element, value) {
        if (value === null || value === undefined) {
            return false;
        }
        if (element.isContentEditable) {
            element.textContent = String(value);
        } else {
            var proto = element.tagName.toLowerCase() === "textarea"
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            var descriptor = Object.getOwnPropertyDescriptor(proto, "value");
            if (descriptor && descriptor.set) {
                descriptor.set.call(element, String(value));
            } else {
                element.value = String(value);
            }
        }
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        return true;
    }

    function fillCredentials(payload) {
        if (!payload || typeof payload !== "object") {
            return false;
        }
        if (payload.auto && state.filled) {
            return false;
        }
        var fields = findFields();
        var filled = {username: false, password: false, totp: false};
        if (fields.username && payload.username) {
            filled.username = setValue(fields.username, payload.username);
        }
        if (fields.password && payload.password) {
            filled.password = setValue(fields.password, payload.password);
        }
        if (fields.totp && payload.totp) {
            filled.totp = setValue(fields.totp, payload.totp);
        }
        if (filled.username || filled.password || filled.totp) {
            state.filled = true;
            send({
                type: "filled",
                payload: {
                    auto: !!payload.auto,
                    username: filled.username,
                    password: filled.password,
                    totp: filled.totp
                }
            });
        }
        hideMenu();
        return true;
    }

    window.__bwFill = fillCredentials;

    // Inline menu ---------------------------------------------------------

    var MENU_CSS = [
        ":host { all: initial; }",
        ".box { box-sizing: border-box; width: 100%; max-height: 260px;",
        "  overflow: auto; background: #ffffff; color: #1f2937;",
        "  border: 1px solid #cbd5e1; border-radius: 6px;",
        "  box-shadow: 0 8px 24px rgba(0,0,0,.22);",
        "  font: 13px/1.4 system-ui, sans-serif; }",
        ".item { display: block; width: 100%; padding: 7px 10px;",
        "  border: 0; background: transparent; text-align: left;",
        "  cursor: pointer; }",
        ".item:hover, .item:focus { background: #eef2ff; outline: none; }",
        ".name { font-weight: 600; }",
        ".user { color: #64748b; margin-left: 6px; }"
    ].join("\n");

    function hideMenu() {
        if (state.menuHost && state.menuHost.parentNode) {
            state.menuHost.parentNode.removeChild(state.menuHost);
        }
        state.menuHost = null;
        state.menuShadow = null;
    }

    function showMenu(items) {
        hideMenu();
        if (!state.activeField || !items || !items.length) {
            return;
        }
        var host = document.createElement("div");
        host.setAttribute("data-qute-bitwarden-menu", "");
        host.style.position = "fixed";
        host.style.zIndex = "2147483647";
        host.tabIndex = -1;
        var shadow = host.attachShadow({mode: "closed"});
        var style = document.createElement("style");
        style.textContent = MENU_CSS;
        shadow.appendChild(style);
        var box = document.createElement("div");
        box.className = "box";
        shadow.appendChild(box);

        items.forEach(function (item) {
            var button = document.createElement("button");
            button.className = "item";
            button.type = "button";
            button.setAttribute("data-item-id", item.id);
            var name = document.createElement("span");
            name.className = "name";
            name.textContent = item.name || "(unnamed)";
            var user = document.createElement("span");
            user.className = "user";
            user.textContent = item.username || "";
            button.appendChild(name);
            button.appendChild(user);
            button.addEventListener("click", function (event) {
                if (!event.isTrusted) {
                    return;
                }
                send({type: "fill", payload: {item_id: item.id}});
            });
            box.appendChild(button);
        });

        var rect = state.activeField.getBoundingClientRect();
        var width = Math.max(220, Math.min(360, rect.width));
        host.style.width = width + "px";
        host.style.left = Math.max(4, Math.min(
            rect.left, window.innerWidth - width - 4)) + "px";
        var below = rect.bottom + 4;
        if (below + 260 > window.innerHeight) {
            below = Math.max(4, rect.top - 264);
        }
        host.style.top = below + "px";

        document.documentElement.appendChild(host);
        state.menuHost = host;
        state.menuShadow = shadow;

        host.addEventListener("keydown", function (event) {
            if (!event.isTrusted) {
                return;
            }
            var buttons = Array.prototype.slice.call(
                shadow.querySelectorAll(".item"));
            var index = buttons.indexOf(shadow.activeElement);
            if (event.key === "Escape") {
                hideMenu();
                event.preventDefault();
            } else if (event.key === "ArrowDown") {
                index = Math.min(buttons.length - 1, index + 1);
                buttons[index].focus();
                event.preventDefault();
            } else if (event.key === "ArrowUp") {
                index = Math.max(0, index - 1);
                buttons[index].focus();
                event.preventDefault();
            } else if (event.key === "Enter" && index >= 0) {
                send({
                    type: "fill",
                    payload: {
                        item_id: buttons[index].getAttribute("data-item-id")
                    }
                });
                event.preventDefault();
            }
        });
        host.focus();
    }

    // Focus + messages ----------------------------------------------------

    function onFocus(event) {
        var target = event.target;
        if (!target || (state.menuHost &&
                state.menuHost.contains(target))) {
            return;
        }
        var kind = classify(target);
        if (!kind || kind === "totp") {
            return;
        }
        state.activeField = target;
        send({
            type: "fieldFocus",
            payload: {url: window.location.href, kind: kind}
        });
    }

    function handleMessage(payload) {
        if (!payload || typeof payload !== "object") {
            return;
        }
        if (payload.type === "candidates") {
            showMenu(payload.items || []);
        } else if (payload.type === "credentials") {
            fillCredentials(payload);
        } else if (payload.type === "showMenu") {
            if (state.activeField) {
                send({
                    type: "fieldFocus",
                    payload: {url: window.location.href, kind: "username"}
                });
            }
        }
    }

    function initMessaging() {
        var api = messaging();
        if (!api) {
            window.setTimeout(initMessaging, 50);
            return;
        }
        api.onMessage("bitwarden", handleMessage);
        document.addEventListener("focusin", onFocus, true);
        document.addEventListener("focusout", function () {
            window.setTimeout(function () {
                if (state.menuHost &&
                        document.activeElement !== state.menuHost &&
                        !state.menuHost.matches(":hover")) {
                    hideMenu();
                }
            }, 150);
        }, true);
        window.addEventListener("resize", hideMenu);
        document.addEventListener("scroll", hideMenu, true);
        window.addEventListener("blur", hideMenu);
    }

    // Page-load autofill: Python drives the first document; JS drives SPA. --

    var autoRequested = {};

    function requestAutoFill() {
        if (state.filled) {
            return;
        }
        var fields = findFields();
        if (!fields.username && !fields.password) {
            return;
        }
        var route = window.location.href;
        if (autoRequested[route]) {
            return;
        }
        autoRequested[route] = true;
        send({
            type: "autoFillRequest",
            payload: {url: route, request_id: ++state.requestId}
        });
    }

    function checkRoute() {
        if (window.location.href === state.route) {
            return;
        }
        state.route = window.location.href;
        state.filled = false;
        window.setTimeout(requestAutoFill, 250);
    }

    window.setInterval(checkRoute, 500);
    window.addEventListener("popstate", checkRoute);
    window.addEventListener("hashchange", checkRoute);

    var observer = new MutationObserver(function () {
        if (state.filled) {
            return;
        }
        window.setTimeout(requestAutoFill, 600);
    });
    observer.observe(document, {
        childList: true,
        subtree: true
    });

    initMessaging();
})();
