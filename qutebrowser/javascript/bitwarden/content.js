// SPDX-FileCopyrightText: qutebrowser contributors
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Bitwarden ApplicationWorld content script: form/field discovery, inline
// login menu, password generation UI, fill, and submit capture. Never sends
// field values to the page world.

(function () {
    "use strict";

    if (window.__quteBitwarden) {
        return;
    }

    var DOCUMENT_FORM_ID = "__document__";
    var BUTTON_SIZE = 22;
    var MENU_WIDTH = 300;

    var state = {
        activeField: null,
        activeFormId: null,
        menuHost: null,
        menuShadow: null,
        generatorButtons: Object.create(null),
        generatorMenuHost: null,
        generatorMenuShadow: null,
        generatorFormId: null,
        generatorAnchor: null,
        generatorPending: null,
        generatorOutsideHandler: null,
        generatorOptions: {
            mode: "password",
            length: 14,
            uppercase: true,
            lowercase: true,
            number: true,
            special: false,
            minNumber: 0,
            minSpecial: 0,
            ambiguous: false,
            words: 3,
            separator: "-",
            capitalize: false,
            includeNumber: false
        },
        filledForms: Object.create(null),
        route: window.location.href,
        requestId: 0,
        lastSaveHash: null,
        lastSaveAt: 0
    };
    window.__quteBitwarden = state;

    var formCounter = 0;
    var formIds = new WeakMap();
    var formElements = Object.create(null);

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

    // Form registry / field discovery -------------------------------------

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

    function autocompleteTokens(element) {
        return attr(element, "autocomplete").split(/\s+/);
    }

    function hasToken(element, token) {
        return autocompleteTokens(element).indexOf(token) !== -1;
    }

    function nearestForm(element) {
        if (element.form) {
            return element.form;
        }
        try {
            return element.closest ? element.closest("form") : null;
        } catch (e) {
            return null;
        }
    }

    function formIdFor(element) {
        var form = nearestForm(element);
        if (!form) {
            return DOCUMENT_FORM_ID;
        }
        var id = formIds.get(form);
        if (!id) {
            id = "form-" + (++formCounter);
            formIds.set(form, id);
            formElements[id] = form;
        }
        return id;
    }

    function classifyField(element, forCapture) {
        if (!element || !isVisible(element)) {
            return null;
        }
        if (!forCapture && (element.disabled || element.readOnly)) {
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
        if (autocomplete === "one-time-code" ||
                /(^|\b)(otp|totp|2fa)(\b|$)/.test(text)) {
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

    function fieldsForForm(formId, forCapture) {
        var root = formId === DOCUMENT_FORM_ID
            ? document : formElements[formId];
        if (!root) {
            return [];
        }
        var elements = [];
        collectRoots(root, elements);
        var fields = [];
        for (var i = 0; i < elements.length; i++) {
            var element = elements[i];
            if (formId === DOCUMENT_FORM_ID && nearestForm(element)) {
                continue;
            }
            var kind = classifyField(element, forCapture);
            if (kind) {
                fields.push({element: element, kind: kind});
            }
        }
        return fields;
    }

    function formTextIntent(formId) {
        var form = formElements[formId];
        var parts = [];
        if (form) {
            parts.push(form.getAttribute("action") || "");
            parts.push(form.getAttribute("name") || "");
            parts.push(form.getAttribute("id") || "");
            var buttons = form.querySelectorAll(
                "button, input[type=submit], input[type=button]");
            for (var i = 0; i < buttons.length; i++) {
                parts.push(buttons[i].textContent || "");
                parts.push(buttons[i].value || "");
            }
        } else {
            parts.push(window.location.pathname);
        }
        return parts.join(" ").toLowerCase();
    }

    function passwordRoles(formId, forCapture) {
        var fields = fieldsForForm(formId, forCapture);
        var passwords = fields.filter(function (field) {
            return field.kind === "password";
        });
        var meta = new Map();
        passwords.forEach(function (field) {
            var element = field.element;
            var name = [attr(element, "name"), attr(element, "id"),
                attr(element, "placeholder"),
                attr(element, "aria-label")].join(" ");
            meta.set(element, {
                role: "unknown",
                explicitCurrent: hasToken(element, "current-password") ||
                    /(^|\b)(old|current|existing)(\b|$)/.test(name),
                explicitNew: hasToken(element, "new-password"),
                confirmName: /(confirm|repeat|retype|verify|again)/.test(name)
            });
        });

        // 1. Explicit current.
        passwords.forEach(function (field) {
            if (meta.get(field.element).explicitCurrent) {
                meta.get(field.element).role = "current";
            }
        });
        // 2. Explicit new; keep only the first new in a form.
        var firstExplicitNew = null;
        passwords.forEach(function (field) {
            var item = meta.get(field.element);
            if (!item.explicitNew) {
                return;
            }
            if (firstExplicitNew === null) {
                item.role = "new";
                firstExplicitNew = field.element;
            } else {
                item.role = "confirm";
            }
        });
        // 3. Confirmation names.
        passwords.forEach(function (field) {
            var item = meta.get(field.element);
            if (item.confirmName && item.role !== "current" &&
                    item.role !== "new") {
                item.role = "confirm";
            }
        });
        // 4. Remaining fields.
        var remaining = passwords.filter(function (field) {
            return meta.get(field.element).role === "unknown";
        });
        if (firstExplicitNew !== null) {
            var seenNew = false;
            passwords.forEach(function (field) {
                var item = meta.get(field.element);
                if (field.element === firstExplicitNew) {
                    seenNew = true;
                    return;
                }
                if (item.role !== "unknown") {
                    return;
                }
                item.role = seenNew ? "confirm" : "current";
            });
        } else {
            var hasConfirm = passwords.some(function (field) {
                return meta.get(field.element).role === "confirm";
            });
            if (hasConfirm && remaining.length === 1) {
                meta.get(remaining[0].element).role = "new";
            } else if (remaining.length >= 3) {
                meta.get(remaining[0].element).role = "current";
                meta.get(remaining[1].element).role = "new";
                for (var i = 2; i < remaining.length; i++) {
                    meta.get(remaining[i].element).role = "confirm";
                }
            } else if (remaining.length === 2) {
                meta.get(remaining[0].element).role = "current";
                meta.get(remaining[1].element).role = "new";
            }
        }

        passwords.forEach(function (field) {
            field.role = meta.get(field.element).role;
            field.explicitCurrent = meta.get(field.element).explicitCurrent;
        });
        return {fields: fields, passwords: passwords, meta: meta};
    }

    window.__bwPasswordRoles = function (formId) {
        return passwordRoles(formId, false).passwords.map(function (field) {
            return {
                id: field.element.id || "",
                name: field.element.name || "",
                role: field.role
            };
        });
    };

    function formSummary(formId, forCapture) {
        var fields = fieldsForForm(formId, forCapture);
        var roles = passwordRoles(formId, forCapture);
        var passwords = roles.passwords;
        var username = fields.some(function (field) {
            return field.kind === "username";
        });
        var totp = fields.some(function (field) {
            return field.kind === "totp";
        });
        var newPassword = passwords.some(function (field) {
            return field.role === "new";
        });
        var confirmPassword = passwords.some(function (field) {
            return field.role === "confirm";
        });
        var passwordCount = passwords.length;
        var strong = passwords.some(function (field) {
            return field.explicitNew;
        }) || passwordCount >= 2;
        var passwordChange = passwords.some(function (field) {
            return field.role === "current";
        }) && passwords.some(function (field) {
            return field.role === "new";
        });
        var intent = formTextIntent(formId);
        var registration = strong ||
            /(^|\b)(register|signup|sign-up|create account)(\b|$)/.test(intent);
        var form = formElements[formId] || null;
        return {
            id: formId,
            action: form ? (form.getAttribute("action") || form.action || null)
                : null,
            username: username,
            password: passwordCount > 0,
            totp: totp,
            new_password: newPassword,
            confirm_password: confirmPassword,
            password_count: passwordCount,
            registration: registration,
            registration_strong: strong,
            password_change: passwordChange
        };
    }

    function allFormIds() {
        var elements = [];
        collectRoots(document, elements);
        var ids = [];
        var seen = Object.create(null);
        for (var i = 0; i < elements.length; i++) {
            var kind = classifyField(elements[i], false);
            if (!kind) {
                continue;
            }
            var id = formIdFor(elements[i]);
            if (!seen[id]) {
                seen[id] = true;
                ids.push(id);
            }
        }
        return ids;
    }

    function eligibleLoginForms() {
        return allFormIds().map(function (id) {
            return formSummary(id, false);
        }).filter(function (summary) {
            return !summary.registration && (summary.username || summary.password);
        });
    }

    function selectedLoginForm() {
        var forms = eligibleLoginForms();
        return forms.length === 1 ? forms[0] : null;
    }

    function collectFields() {
        var forms = allFormIds().map(function (id) {
            return formSummary(id, false);
        });
        var selected = selectedLoginForm();
        var result = {
            url: window.location.href,
            form_id: selected ? selected.id : null,
            form_action: selected ? selected.action : null,
            username: selected ? selected.username : false,
            password: selected ? selected.password : false,
            totp: selected ? selected.totp : false,
            new_password: selected ? selected.new_password : false,
            confirm_password: selected ? selected.confirm_password : false,
            password_count: selected ? selected.password_count : 0,
            registration: selected ? selected.registration : false,
            registration_strong: selected ? selected.registration_strong : false,
            password_change: selected ? selected.password_change : false,
            forms: forms
        };
        return result;
    }

    window.__bwCollectFields = collectFields;

    function findTotpField() {
        var elements = [];
        collectRoots(document, elements);
        for (var i = 0; i < elements.length; i++) {
            if (classifyField(elements[i], false) === "totp") {
                return elements[i];
            }
        }
        return null;
    }

    // Filling -------------------------------------------------------------

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
        var formId = payload.form_id || null;
        if (!formId) {
            var selected = selectedLoginForm();
            if (!selected) {
                return false;
            }
            formId = selected.id;
        }
        var summary = formSummary(formId, false);
        if (summary.registration) {
            return false;
        }
        if (payload.auto && state.filledForms[formId]) {
            return false;
        }
        var fields = fieldsForForm(formId, false);
        var roles = passwordRoles(formId, false);
        var usernameField = fields.find(function (field) {
            return field.kind === "username";
        });
        var passwordField = roles.passwords.find(function (field) {
            return field.role !== "new" && field.role !== "confirm";
        });
        var filled = {username: false, password: false, totp: false};
        if (usernameField && payload.username) {
            filled.username = setValue(usernameField.element, payload.username);
        }
        if (passwordField && payload.password) {
            filled.password = setValue(passwordField.element, payload.password);
        }
        var totpField = findTotpField();
        if (totpField && payload.totp) {
            filled.totp = setValue(totpField, payload.totp);
        }
        if (filled.username || filled.password || filled.totp) {
            state.filledForms[formId] = true;
            send({
                type: "filled",
                payload: {
                    auto: !!payload.auto,
                    generated: !!payload.generated,
                    form_id: formId,
                    username: filled.username,
                    password: filled.password,
                    totp: filled.totp
                }
            });
        }
        hideMenu();
        closeGeneratorMenu();
        return true;
    }

    window.__bwFill = fillCredentials;

    // Inline login menu ---------------------------------------------------

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

    function showMenu(items, formId) {
        hideMenu();
        if (!state.activeField || !items || !items.length) {
            return;
        }
        if (formId && formId !== state.activeFormId) {
            return;
        }
        var summary = formSummary(state.activeFormId || DOCUMENT_FORM_ID, false);
        if (summary.registration) {
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
                send({
                    type: "fill",
                    payload: {
                        item_id: item.id,
                        form_id: state.activeFormId
                    }
                });
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
                        item_id: buttons[index].getAttribute("data-item-id"),
                        form_id: state.activeFormId
                    }
                });
                event.preventDefault();
            }
        });
        host.focus();
    }

    // Generator button / menu ---------------------------------------------

    var GENERATOR_CSS = [
        ":host { all: initial; }",
        "button.gen { width: 22px; height: 22px; padding: 0;",
        "  border: 1px solid #cbd5e1; border-radius: 4px;",
        "  background: #f8fafc; color: #334155;",
        "  font: 600 13px/20px system-ui, sans-serif; cursor: pointer;",
        "  box-shadow: 0 1px 3px rgba(0,0,0,.18); }",
        "button.gen:hover, button.gen:focus { background: #eef2ff;",
        "  outline: 2px solid #818cf8; outline-offset: 1px; }",
        ".menu { box-sizing: border-box; width: 100%; max-height: 520px;",
        "  overflow: auto; background: #ffffff; color: #1f2937;",
        "  border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px;",
        "  box-shadow: 0 10px 30px rgba(0,0,0,.25);",
        "  font: 13px/1.4 system-ui, sans-serif; }",
        ".tabs { display: flex; gap: 6px; margin-bottom: 10px; }",
        ".tabs button { flex: 1; padding: 5px 8px; border: 1px solid #cbd5e1;",
        "  border-radius: 5px; background: #f8fafc; cursor: pointer; }",
        ".tabs button.active { background: #e0e7ff; border-color: #818cf8;",
        "  font-weight: 600; }",
        ".row { display: flex; align-items: center; justify-content: space-between;",
        "  gap: 10px; margin: 6px 0; }",
        ".row label { flex: 1; }",
        "input[type=number], input[type=text], select { width: 92px;",
        "  box-sizing: border-box; padding: 3px 5px; border: 1px solid #cbd5e1;",
        "  border-radius: 4px; font: inherit; }",
        ".check { display: flex; align-items: center; gap: 6px; margin: 6px 0; }",
        ".actions { display: flex; gap: 8px; margin-top: 12px; }",
        ".actions button { flex: 1; padding: 7px 8px; border: 1px solid #4f46e5;",
        "  border-radius: 5px; background: #4f46e5; color: white;",
        "  font: inherit; cursor: pointer; }",
        ".actions button:disabled { opacity: .55; cursor: default; }",
        ".error { color: #b91c1c; min-height: 18px; margin-top: 6px; }"
    ].join("\n");

    function generatorAnchorFor(formId) {
        var roles = passwordRoles(formId, false);
        for (var i = 0; i < roles.passwords.length; i++) {
            if (roles.passwords[i].role === "new") {
                return roles.passwords[i].element;
            }
        }
        return null;
    }

    function positionGeneratorButton(item, anchor) {
        if (!anchor || !anchor.isConnected || !isVisible(anchor)) {
            item.host.style.display = "none";
            return;
        }
        var rect = anchor.getBoundingClientRect();
        item.host.style.display = "block";
        item.host.style.left =
            (rect.right - BUTTON_SIZE - 4) + "px";
        item.host.style.top =
            (rect.top + (rect.height - BUTTON_SIZE) / 2) + "px";
    }

    function positionGeneratorButtons() {
        Object.keys(state.generatorButtons).forEach(function (formId) {
            var item = state.generatorButtons[formId];
            positionGeneratorButton(item, item.anchor);
        });
    }

    function requestButtonPosition() {
        if (state.buttonFrame) {
            return;
        }
        state.buttonFrame = window.requestAnimationFrame(function () {
            state.buttonFrame = null;
            positionGeneratorButtons();
        });
    }

    function removeGeneratorButton(formId) {
        var item = state.generatorButtons[formId];
        if (!item) {
            return;
        }
        if (item.host.parentNode) {
            item.host.parentNode.removeChild(item.host);
        }
        delete state.generatorButtons[formId];
    }

    function ensureGeneratorButton(formId, anchor) {
        var existing = state.generatorButtons[formId];
        if (existing) {
            existing.anchor = anchor;
            positionGeneratorButton(existing, anchor);
            return;
        }
        var host = document.createElement("div");
        host.setAttribute("data-qute-bitwarden-generate", "");
        host.style.position = "fixed";
        host.style.zIndex = "2147483647";
        host.style.pointerEvents = "none";
        var shadow = host.attachShadow({mode: "closed"});
        var style = document.createElement("style");
        style.textContent = GENERATOR_CSS;
        shadow.appendChild(style);
        var button = document.createElement("button");
        button.type = "button";
        button.className = "gen";
        button.textContent = "G";
        button.title = "Generate password";
        button.setAttribute("aria-label", "Generate password");
        button.style.pointerEvents = "auto";
        button.addEventListener("click", function (event) {
            if (!event.isTrusted) {
                return;
            }
            event.preventDefault();
            event.stopPropagation();
            openGeneratorMenu(formId, anchor, host);
        });
        shadow.appendChild(button);
        document.documentElement.appendChild(host);
        state.generatorButtons[formId] = {
            host: host, shadow: shadow, button: button, anchor: anchor
        };
        positionGeneratorButton(state.generatorButtons[formId], anchor);
    }

    function scanGeneratorButtons() {
        if (window.top !== window) {
            Object.keys(state.generatorButtons).forEach(removeGeneratorButton);
            return;
        }
        var ids = allFormIds();
        var seen = Object.create(null);
        ids.forEach(function (formId) {
            var summary = formSummary(formId, false);
            var anchor = summary.registration ? generatorAnchorFor(formId) : null;
            if (anchor) {
                seen[formId] = true;
                ensureGeneratorButton(formId, anchor);
            }
        });
        Object.keys(state.generatorButtons).forEach(function (formId) {
            if (!seen[formId]) {
                removeGeneratorButton(formId);
            }
        });
        positionGeneratorButtons();
    }

    function optionElement(tag, className, text) {
        var element = document.createElement(tag);
        if (className) {
            element.className = className;
        }
        if (text !== undefined) {
            element.textContent = text;
        }
        return element;
    }

    function optionRow(labelText, control) {
        var row = optionElement("div", "row");
        row.appendChild(optionElement("label", "", labelText));
        row.appendChild(control);
        return row;
    }

    function optionCheck(labelText, key) {
        var label = optionElement("label", "check");
        var input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!state.generatorOptions[key];
        input.addEventListener("change", function () {
            state.generatorOptions[key] = input.checked;
        });
        label.appendChild(input);
        label.appendChild(document.createTextNode(labelText));
        return label;
    }

    function numberInput(key, min) {
        var input = document.createElement("input");
        input.type = "number";
        input.min = String(min);
        input.value = String(state.generatorOptions[key]);
        input.addEventListener("input", function () {
            var value = parseInt(input.value, 10);
            state.generatorOptions[key] = isNaN(value) ? min : value;
        });
        return input;
    }

    function renderGeneratorMenu() {
        if (!state.generatorMenuShadow) {
            return;
        }
        var shadow = state.generatorMenuShadow;
        var menu = optionElement("div", "menu");
        var tabs = optionElement("div", "tabs");
        ["password", "passphrase"].forEach(function (mode) {
            var button = optionElement("button", "", mode === "password"
                ? "Password" : "Passphrase");
            button.type = "button";
            if (state.generatorOptions.mode === mode) {
                button.className += " active";
            }
            button.addEventListener("click", function () {
                state.generatorOptions.mode = mode;
                renderGeneratorMenu();
            });
            tabs.appendChild(button);
        });
        menu.appendChild(tabs);

        if (state.generatorOptions.mode === "password") {
            menu.appendChild(optionRow("Length", numberInput("length", 5)));
            menu.appendChild(optionCheck("Uppercase", "uppercase"));
            menu.appendChild(optionCheck("Lowercase", "lowercase"));
            menu.appendChild(optionCheck("Numbers", "number"));
            menu.appendChild(optionCheck("Special", "special"));
            menu.appendChild(optionRow("Min numbers", numberInput("minNumber", 0)));
            menu.appendChild(optionRow("Min special", numberInput("minSpecial", 0)));
            menu.appendChild(optionCheck("Avoid ambiguous", "ambiguous"));
        } else {
            menu.appendChild(optionRow("Words", numberInput("words", 3)));
            var separator = document.createElement("select");
            [["-", "-"], ["_", "_"], ["space", "space"],
                ["empty", "empty"], [".", "."], ["custom", "custom"]]
                .forEach(function (entry) {
                    var option = document.createElement("option");
                    option.value = entry[0];
                    option.textContent = entry[1];
                    if (entry[0] === state.generatorOptions.separator) {
                        option.selected = true;
                    }
                    separator.appendChild(option);
                });
            separator.addEventListener("change", function () {
                if (separator.value === "custom") {
                    var custom = window.prompt("Separator character", "-");
                    state.generatorOptions.separator =
                        custom && custom.length === 1 ? custom : "-";
                } else {
                    state.generatorOptions.separator = separator.value;
                }
            });
            menu.appendChild(optionRow("Separator", separator));
            menu.appendChild(optionCheck("Capitalize", "capitalize"));
            menu.appendChild(optionCheck("Include number", "includeNumber"));
        }

        var error = optionElement("div", "error", "");
        error.setAttribute("data-generator-error", "");
        menu.appendChild(error);
        var actions = optionElement("div", "actions");
        var generate = optionElement("button", "", "Generate");
        generate.type = "button";
        if (state.generatorPending) {
            generate.disabled = true;
            generate.textContent = "Generating...";
        }
        generate.addEventListener("click", function (event) {
            if (!event.isTrusted) {
                return;
            }
            event.preventDefault();
            startGeneration();
        });
        actions.appendChild(generate);
        menu.appendChild(actions);
        shadow.innerHTML = "";
        var style = document.createElement("style");
        style.textContent = GENERATOR_CSS;
        shadow.appendChild(style);
        shadow.appendChild(menu);
    }

    function setGeneratorError(message) {
        if (!state.generatorMenuShadow) {
            return;
        }
        var error = state.generatorMenuShadow.querySelector(
            "[data-generator-error]");
        if (error) {
            error.textContent = message || "";
        }
    }

    function openGeneratorMenu(formId, anchor, buttonHost) {
        hideMenu();
        closeGeneratorMenu();
        state.generatorFormId = formId;
        state.generatorAnchor = anchor;
        var host = document.createElement("div");
        host.setAttribute("data-qute-bitwarden-generator-menu", "");
        host.style.position = "fixed";
        host.style.zIndex = "2147483647";
        host.style.width = MENU_WIDTH + "px";
        host.tabIndex = -1;
        var shadow = host.attachShadow({mode: "closed"});
        state.generatorMenuHost = host;
        state.generatorMenuShadow = shadow;
        document.documentElement.appendChild(host);
        renderGeneratorMenu();
        var rect = buttonHost.getBoundingClientRect();
        host.style.left = Math.max(4, Math.min(
            rect.left, window.innerWidth - MENU_WIDTH - 4)) + "px";
        var below = rect.bottom + 4;
        if (below + 520 > window.innerHeight) {
            below = Math.max(4, rect.top - 524);
        }
        host.style.top = below + "px";
        host.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                closeGeneratorMenu();
                event.preventDefault();
            }
        });
        state.generatorOutsideHandler = function (event) {
            if (!event.isTrusted) {
                return;
            }
            var path = event.composedPath ? event.composedPath() : [];
            if (path.indexOf(host) === -1) {
                closeGeneratorMenu();
            }
        };
        document.addEventListener(
            "pointerdown", state.generatorOutsideHandler, true);
        window.setTimeout(function () {
            var first = shadow.querySelector("button");
            if (first) {
                first.focus();
            }
        }, 0);
    }

    function closeGeneratorMenu() {
        if (state.generatorPending) {
            window.clearTimeout(state.generatorPending.timer);
            state.generatorPending = null;
        }
        if (state.generatorOutsideHandler) {
            document.removeEventListener(
                "pointerdown", state.generatorOutsideHandler, true);
            state.generatorOutsideHandler = null;
        }
        if (state.generatorMenuHost &&
                state.generatorMenuHost.parentNode) {
            state.generatorMenuHost.parentNode.removeChild(
                state.generatorMenuHost);
        }
        state.generatorMenuHost = null;
        state.generatorMenuShadow = null;
        state.generatorFormId = null;
        state.generatorAnchor = null;
    }

    function startGeneration() {
        if (state.generatorPending || !state.generatorFormId) {
            return;
        }
        var options = JSON.parse(JSON.stringify(state.generatorOptions));
        if (options.mode === "password") {
            if (!(options.uppercase || options.lowercase ||
                    options.number || options.special)) {
                setGeneratorError("Select at least one character set.");
                return;
            }
            if (!(options.length >= 5)) {
                setGeneratorError("Length must be at least 5.");
                return;
            }
        } else if (!(options.words >= 3)) {
            setGeneratorError("Use at least 3 words.");
            return;
        }
        var requestId = ++state.requestId;
        var formId = state.generatorFormId;
        state.generatorPending = {
            requestId: requestId,
            formId: formId,
            timer: window.setTimeout(function () {
                state.generatorPending = null;
                setGeneratorError("Bitwarden generator timed out.");
                renderGeneratorMenu();
            }, 15000)
        };
        renderGeneratorMenu();
        if (!send({
            type: "generatePassword",
            payload: {
                request_id: requestId,
                form_id: formId,
                options: options
            }
        })) {
            state.generatorPending = null;
            setGeneratorError("Bitwarden messaging is unavailable.");
            renderGeneratorMenu();
        }
    }

    function fillGenerated(formId, password) {
        var roles = passwordRoles(formId, false);
        var targets = roles.passwords.filter(function (field) {
            return field.role === "new" || field.role === "confirm";
        });
        if (!targets.length) {
            return false;
        }
        var filled = false;
        targets.forEach(function (field) {
            filled = setValue(field.element, password) || filled;
        });
        if (!filled) {
            return false;
        }
        state.filledForms[formId] = true;
        send({
            type: "filled",
            payload: {
                auto: false,
                generated: true,
                form_id: formId,
                username: false,
                password: true,
                totp: false
            }
        });
        if (state.generatorAnchor && state.generatorAnchor.focus) {
            state.generatorAnchor.focus();
        }
        return true;
    }

    // Save capture --------------------------------------------------------

    function resolveSubmitForm(event) {
        var target = event.target;
        if (target && target.tagName &&
                target.tagName.toLowerCase() === "form") {
            return target;
        }
        var path = event.composedPath ? event.composedPath() : [target];
        for (var i = 0; i < path.length; i++) {
            if (path[i] && path[i].tagName &&
                    path[i].tagName.toLowerCase() === "form") {
                return path[i];
            }
        }
        if (target && target.closest) {
            return target.closest("form");
        }
        return null;
    }

    function saveCandidate(form) {
        if (!form || !form.isConnected) {
            return;
        }
        var formId = formIdFor(form);
        var summary = formSummary(formId, true);
        if (!summary.password) {
            return;
        }
        var roles = passwordRoles(formId, true);
        var fields = roles.fields;
        var usernameField = fields.find(function (field) {
            return field.kind === "username";
        });
        var username = usernameField ? usernameField.element.value : "";
        var passwordField = null;
        if (summary.registration) {
            passwordField = roles.passwords.find(function (field) {
                return field.role === "new";
            });
        } else {
            passwordField = roles.passwords[0];
        }
        if (!passwordField) {
            return;
        }
        var password = passwordField.element.value;
        if (!password) {
            return;
        }
        if (summary.registration) {
            if (!summary.registration_strong) {
                return;
            }
            for (var i = 0; i < roles.passwords.length; i++) {
                var field = roles.passwords[i];
                if (field.element === passwordField.element ||
                        field.explicitCurrent) {
                    continue;
                }
                var value = field.element.value;
                if (value && value !== password) {
                    return;
                }
            }
        }
        var url = window.location.href;
        if (!/^https?:\/\//.test(url)) {
            return;
        }
        var hash = [url, username, password, formId].join("\n");
        var now = Date.now();
        if (state.lastSaveHash === hash && now - state.lastSaveAt < 5000) {
            return;
        }
        state.lastSaveHash = hash;
        state.lastSaveAt = now;
        send({
            type: "saveCandidate",
            payload: {
                url: url,
                username: username,
                password: password,
                registration: summary.registration,
                registration_strong: summary.registration_strong,
                password_change: summary.password_change,
                form_id: formId,
                form_action: summary.action
            }
        });
    }

    function onSubmit(event) {
        if (window.top !== window) {
            return;
        }
        var form = resolveSubmitForm(event);
        if (!form) {
            return;
        }
        queueMicrotask(function () {
            if (event.defaultPrevented) {
                return;
            }
            saveCandidate(form);
        });
    }

    // Focus + messages ----------------------------------------------------

    function isOurUi(element) {
        if (!element) {
            return false;
        }
        if (state.menuHost && state.menuHost.contains(element)) {
            return true;
        }
        if (state.generatorMenuHost &&
                state.generatorMenuHost.contains(element)) {
            return true;
        }
        return false;
    }

    function onFocus(event) {
        var target = event.target;
        if (!target || isOurUi(target)) {
            return;
        }
        var kind = classifyField(target, false);
        if (!kind || kind === "totp") {
            return;
        }
        var formId = formIdFor(target);
        var summary = formSummary(formId, false);
        state.activeField = target;
        state.activeFormId = formId;
        if (summary.registration) {
            hideMenu();
            scanGeneratorButtons();
            return;
        }
        send({
            type: "fieldFocus",
            payload: {
                url: window.location.href,
                kind: kind,
                form_id: formId,
                form: summary
            }
        });
        scanGeneratorButtons();
    }

    function handleMessage(payload) {
        if (!payload || typeof payload !== "object") {
            return;
        }
        if (payload.type === "candidates") {
            showMenu(payload.items || [], payload.form_id);
        } else if (payload.type === "credentials") {
            fillCredentials(payload);
        } else if (payload.type === "generatedPassword") {
            if (!state.generatorPending ||
                    payload.request_id !== state.generatorPending.requestId ||
                    payload.form_id !== state.generatorPending.formId) {
                return;
            }
            window.clearTimeout(state.generatorPending.timer);
            state.generatorPending = null;
            var formId = payload.form_id;
            if (!fillGenerated(formId, payload.password)) {
                setGeneratorError("No new-password field found.");
                renderGeneratorMenu();
                return;
            }
            closeGeneratorMenu();
        } else if (payload.type === "generateError") {
            if (!state.generatorPending ||
                    payload.request_id !== state.generatorPending.requestId ||
                    payload.form_id !== state.generatorPending.formId) {
                return;
            }
            window.clearTimeout(state.generatorPending.timer);
            state.generatorPending = null;
            setGeneratorError(payload.error || "Generation failed.");
            renderGeneratorMenu();
        } else if (payload.type === "showMenu") {
            if (state.activeField) {
                send({
                    type: "fieldFocus",
                    payload: {
                        url: window.location.href,
                        kind: "username",
                        form_id: state.activeFormId,
                        form: formSummary(state.activeFormId, false)
                    }
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
        document.addEventListener("submit", onSubmit, false);
        window.addEventListener("resize", function () {
            requestButtonPosition();
        });
        document.addEventListener("scroll", requestButtonPosition, true);
        window.addEventListener("blur", hideMenu);
    }

    // Page-load autofill: Python drives the first document; JS drives SPA. --

    var autoRequested = Object.create(null);
    var mutationTimer = null;

    function requestAutoFill() {
        var selected = selectedLoginForm();
        if (!selected) {
            return;
        }
        var key = selected.id + "|" + window.location.href;
        if (autoRequested[key] || state.filledForms[selected.id]) {
            return;
        }
        autoRequested[key] = true;
        send({
            type: "autoFillRequest",
            payload: {
                url: window.location.href,
                form_id: selected.id,
                form: selected,
                request_id: ++state.requestId
            }
        });
    }

    window.__bwRequestAutoFill = requestAutoFill;

    function checkRoute() {
        if (window.location.href === state.route) {
            return;
        }
        state.route = window.location.href;
        state.filledForms = Object.create(null);
        scanGeneratorButtons();
        window.setTimeout(requestAutoFill, 250);
    }

    window.setInterval(checkRoute, 500);
    window.addEventListener("popstate", checkRoute);
    window.addEventListener("hashchange", checkRoute);

    var observer = new MutationObserver(function () {
        if (mutationTimer) {
            return;
        }
        mutationTimer = window.setTimeout(function () {
            mutationTimer = null;
            scanGeneratorButtons();
            requestAutoFill();
        }, 300);
    });
    observer.observe(document, {
        childList: true,
        subtree: true
    });

    initMessaging();
    window.setTimeout(scanGeneratorButtons, 0);
})();
