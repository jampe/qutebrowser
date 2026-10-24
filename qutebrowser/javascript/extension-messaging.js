// SPDX-FileCopyrightText: qutebrowser contributors
//
// SPDX-License-Identifier: GPL-3.0-or-later

// ApplicationWorld messaging shim. This file is concatenated after
// qwebchannel.js and wrapped by javascript.wrap_global(), so QWebChannel and
// window._qutebrowser already exist in this scope.

(function () {
    "use strict";

    var ns = window._qutebrowser;
    ns.extensions = ns.extensions || {};
    if (ns.extensions.messaging) {
        return;
    }

    var isTop = (window === window.top);
    var token = (function () {
        var bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        var out = "";
        for (var i = 0; i < bytes.length; i++) {
            out += ("0" + bytes[i].toString(16)).slice(-2);
        }
        return out;
    })();

    var state = {
        channel: null,
        ready: false,
        queue: [],
        callbacks: {},
        readyCallbacks: []
    };

    function origin() {
        return window.location.origin;
    }

    function deliver(message) {
        var callbacks = state.callbacks[message.channel] || [];
        for (var i = 0; i < callbacks.length; i++) {
            try {
                callbacks[i](message.payload);
            } catch (e) {
                // A component callback must not break the shim.
            }
        }
    }

    function sendRaw(envelope) {
        if (!state.ready) {
            if (state.queue.length < 100) {
                state.queue.push(envelope);
            }
            return;
        }
        try {
            state.channel.objects.qutebrowser.send(JSON.stringify(envelope));
        } catch (e) {
            // Ignore transport errors; Python logs metadata only.
        }
    }

    function send(message) {
        if (!message || typeof message !== "object") {
            return;
        }
        var envelope = {};
        for (var key in message) {
            if (Object.prototype.hasOwnProperty.call(message, key)) {
                envelope[key] = message[key];
            }
        }
        envelope.token = token;
        envelope.origin = origin();
        envelope.relayed = false;
        if (isTop) {
            sendRaw(envelope);
        } else {
            try {
                window.top.postMessage(
                    {quteExt: true, token: token, message: envelope}, "*");
            } catch (e) {
                // Cross-origin access can fail if the top frame is gone.
            }
        }
    }

    function onMessage(channel, callback) {
        if (!state.callbacks[channel]) {
            state.callbacks[channel] = [];
        }
        state.callbacks[channel].push(callback);
    }

    function onReady(callback) {
        if (state.ready) {
            callback();
        } else {
            state.readyCallbacks.push(callback);
        }
    }

    var attempts = 0;
    function initTop() {
        if (typeof qt === "undefined" || !qt.webChannelTransport) {
            attempts += 1;
            if (attempts < 50) {
                window.setTimeout(initTop, 20);
            }
            return;
        }
        new QWebChannel(qt.webChannelTransport, function (channel) {
            state.channel = channel;
            state.ready = true;
            var queued = state.queue;
            state.queue = [];
            for (var i = 0; i < queued.length; i++) {
                try {
                    channel.objects.qutebrowser.send(JSON.stringify(queued[i]));
                } catch (e) {
                    // Drop; do not log envelope contents.
                }
            }
            var callbacks = state.readyCallbacks;
            state.readyCallbacks = [];
            for (var j = 0; j < callbacks.length; j++) {
                try {
                    callbacks[j]();
                } catch (e) {
                    // Component callback failure is isolated.
                }
            }
        });
    }

    if (isTop) {
        // Must be registered synchronously at DocumentCreation, capture phase,
        // before any page script can register a listener.
        window.addEventListener("message", function (event) {
            var data = event.data;
            if (!data || !data.quteExt || typeof data.token !== "string") {
                return;
            }
            if (event.source === window) {
                return;
            }
            var message = data.message || {};
            var envelope = {};
            for (var key in message) {
                if (Object.prototype.hasOwnProperty.call(message, key)) {
                    envelope[key] = message[key];
                }
            }
            envelope.token = data.token;
            envelope.origin = event.origin;
            envelope.relayed = true;
            sendRaw(envelope);
        }, true);
        initTop();
    }

    ns.extensions.messaging = {
        token: token,
        send: send,
        onMessage: onMessage,
        onReady: onReady,
        _receive: function (message) {
            deliver(message);
        },
        get ready() {
            return state.ready;
        }
    };
})();
