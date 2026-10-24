# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Extension messaging between ApplicationWorld scripts and Python.

The channel is bound to qutebrowser's ApplicationWorld. QWebChannel objects
must be registered before the JS client initializes, so the bridge is created
inert (``enabled=False``) at tab attach and only enabled after a runtime
isolation probe passes. Inbound messages are dropped while the bridge is inert.
"""

import functools
import json
import re
import weakref
from dataclasses import dataclass
from typing import Any, Callable

from qutebrowser.qt.core import QObject, QTimer, pyqtSlot

from qutebrowser.browser import browsertab
from qutebrowser.qt import machinery, sip
from qutebrowser.qt.webchannel import QWebChannel
from qutebrowser.qt.webenginecore import QWebEngineScript
from qutebrowser.utils import javascript, log, resources, usertypes, utils

_MAX_MESSAGE_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r'^[0-9a-f]{32}$')
if machinery.IS_QT6:
    _APPLICATION_WORLD = int(
        QWebEngineScript.ScriptWorldId.ApplicationWorld)
else:  # Qt 5
    _APPLICATION_WORLD = int(QWebEngineScript.ApplicationWorld)
_SHIM_NAME = 'qute_extension_messaging'

_TOKEN_JS = """JSON.stringify([
    (window._qutebrowser &&
     window._qutebrowser.extensions &&
     window._qutebrowser.extensions.messaging &&
     window._qutebrowser.extensions.messaging.token) || null,
    window.location.origin
])"""


@dataclass(frozen=True)
class Message:

    """A message delivered from an ApplicationWorld script."""

    tab: Any
    frame: Any
    origin: str | None
    relayed: bool
    is_main: bool
    channel: str
    type: str
    request_id: str | None
    payload: dict[str, Any]


_handlers: dict[str, tuple[Callable[[Message], None], bool]] = {}


def register(
        channel: str,
        handler: Callable[[Message], None], *,
        allow_relay: bool = False,
) -> None:
    """Register a handler for a channel.

    Channel names starting with ``qute_`` are reserved for qutebrowser.
    """
    if not isinstance(channel, str) or not channel:
        log.extensions.error("Invalid messaging channel name: {!r}".format(
            channel))
        return
    if channel.startswith('qute_'):
        log.extensions.error(
            "Messaging channel {!r} uses the reserved 'qute_' prefix".format(
                channel))
        return
    if channel in _handlers:
        log.extensions.error(
            "Messaging channel {!r} is already registered".format(channel))
        return
    _handlers[channel] = (handler, allow_relay)


def has_handlers() -> bool:
    return bool(_handlers)


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant {!r}".format(value))


class MessageBridge(QObject):

    """Per-tab QWebChannel object and dispatcher."""

    def __init__(self, tab: Any) -> None:
        super().__init__(tab)
        self._tab_ref = weakref.ref(tab)
        self.enabled = False
        self._token_to_frame: dict[str, Any] = {}
        self._frame_origin: dict[int, str | None] = {}
        self._pending: list[dict[str, Any]] = []
        self._scanning = False
        self._negative: dict[str, int] = {}
        self._negative_timer = QTimer(self)
        self._negative_timer.setInterval(5000)
        self._negative_timer.timeout.connect(self._clear_negative)
        self._negative_timer.start()

    def _tab(self) -> Any:
        tab = self._tab_ref()
        if tab is None or sip.isdeleted(tab):
            raise browsertab.WebTabError("Tab is gone")
        return tab

    def _clear_negative(self) -> None:
        self._negative.clear()

    @pyqtSlot(str)
    @utils.prevent_exceptions(None)
    def send(self, message: str) -> None:  # noqa: C901
        """Receive a JSON message from JS."""
        if not self.enabled:
            return
        if len(message.encode('utf-8')) > _MAX_MESSAGE_BYTES:
            log.extensions.warning("Dropping oversized extension message")
            return
        try:
            data = json.loads(message, parse_constant=_reject_constant)
        except (ValueError, json.JSONDecodeError):
            log.extensions.warning("Dropping invalid extension message")
            return
        if not isinstance(data, dict):
            return

        token = data.get('token')
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return
        if token in self._negative:
            return

        channel = data.get('channel')
        message_type = data.get('type')
        request_id = data.get('request_id')
        origin = data.get('origin')
        relayed = data.get('relayed', False)
        payload = data.get('payload')

        if not isinstance(channel, str) or len(channel) > 64:
            return
        if not isinstance(message_type, str) or len(message_type) > 64:
            return
        if request_id is not None and not isinstance(request_id, str):
            return
        if origin is not None and not isinstance(origin, str):
            return
        if not isinstance(relayed, bool):
            return
        if not isinstance(payload, dict):
            return

        self._enqueue(data)

    def _enqueue(self, data: dict[str, Any]) -> None:
        token = data['token']
        raw = self._token_to_frame.get(token)
        if raw is not None:
            if self._frame_is_valid(raw):
                self._dispatch(data)
                return
            self._token_to_frame.pop(token, None)

        if self._scanning:
            if len(self._pending) < 100:
                self._pending.append(data)
            return
        self._pending.append(data)
        self._scan(token)

    def _frame_is_valid(self, raw: Any) -> bool:
        try:
            return bool(raw.isValid())
        except (AttributeError, RuntimeError):
            return False

    def _scan(self, token: str) -> None:  # noqa: C901
        """Resolve a token to a frame by querying each frame."""
        self._scanning = True
        tab = self._tab()
        frames = tab.frames()
        log.extensions.debug("Scanning %d frames for token %s",
                             len(frames), token[:8])
        if not frames:
            self._scanning = False
            self._pending.clear()
            self._negative[token] = 1
            log.extensions.debug(
                "Dropping extension message: no frames available")
            return

        remaining = len(frames)
        finished = False
        found: dict[str, tuple[Any, str | None]] = {}

        def finish() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            for frame_token, (raw, origin) in found.items():
                if origin is None:
                    continue
                self._token_to_frame[frame_token] = raw
                self._frame_origin[id(raw)] = origin
            if token not in found:
                self._negative[token] = 1
            self._scanning = False
            pending = self._pending
            self._pending = []
            for data in pending:
                raw = self._token_to_frame.get(data['token'])
                if raw is not None and self._frame_is_valid(raw):
                    try:
                        self._dispatch(data)
                    except browsertab.WebTabError:
                        return

        def on_result(result: Any, raw: Any = None) -> None:
            nonlocal remaining
            if finished:
                return
            try:
                decoded = json.loads(result) if isinstance(result, str) else None
            except (ValueError, json.JSONDecodeError):
                decoded = None
            if (isinstance(decoded, list) and len(decoded) == 2
                    and isinstance(decoded[0], str)
                    and _TOKEN_RE.fullmatch(decoded[0])):
                if (isinstance(decoded[1], str)
                        and decoded[1] not in ('', 'null')):
                    found[decoded[0]] = (raw, decoded[1])
                else:
                    found[decoded[0]] = (raw, None)
            remaining -= 1
            if remaining <= 0:
                finish()

        for raw in frames:
            if not self._frame_is_valid(raw):
                remaining -= 1
                if remaining <= 0:
                    finish()
                continue
            try:
                raw.runJavaScript(
                    _TOKEN_JS, _APPLICATION_WORLD,
                    functools.partial(on_result, raw=raw))
            except (RuntimeError, AttributeError):
                remaining -= 1
                if remaining <= 0:
                    finish()

        QTimer.singleShot(500, finish)

    def _dispatch(self, data: dict[str, Any]) -> None:
        token = data['token']
        raw = self._token_to_frame.get(token)
        if raw is None:
            return
        expected_origin = self._frame_origin.get(id(raw))
        if expected_origin is None:
            return
        if data.get('origin') != expected_origin:
            return

        channel = data['channel']
        handler_info = _handlers.get(channel)
        if handler_info is None:
            return
        handler, allow_relay = handler_info
        if data.get('relayed') and not allow_relay:
            return
        log.extensions.debug("Dispatching %s relayed=%s channel=%s",
                             data['type'], data.get('relayed'),
                             data['channel'])

        # Import lazily to avoid a browsertab -> messaging cycle.
        from qutebrowser.api import apitypes

        tab = self._tab()
        adapter = tab.api_adapter()
        frame = apitypes.Frame(adapter, raw)
        frame._origin_override = expected_origin
        message = Message(
            tab=adapter,
            frame=frame,
            origin=expected_origin,
            relayed=bool(data.get('relayed')),
            is_main=frame.is_main,
            channel=channel,
            type=data['type'],
            request_id=data.get('request_id'),
            payload=data['payload'],
        )
        QTimer.singleShot(0, functools.partial(self._invoke, handler, message))

    @staticmethod
    def _invoke(handler: Callable[[Message], None], message: Message) -> None:
        try:
            handler(message)
        except BaseException:  # noqa: B036
            log.extensions.exception("Error in extension message handler")

    def post(self, payload: dict[str, Any], raw_frame: Any = None) -> None:
        """Deliver a payload to one frame, guarded by its document token."""
        if not self.enabled:
            raise browsertab.UnsupportedOperationError(
                "Extension messaging is not available")
        if not isinstance(payload, dict):
            raise browsertab.WebTabError("Message payload must be a dict")
        encoded = json.dumps(payload)
        if len(encoded.encode('utf-8')) > _MAX_MESSAGE_BYTES:
            raise browsertab.WebTabError("Message payload is too large")

        tab = self._tab()
        if raw_frame is None:
            frames = tab.frames()
            if frames:
                raw_frame = frames[0]
            else:
                page = tab._widget.page()
                page.runJavaScript(
                    _TOKEN_JS, _APPLICATION_WORLD,
                    functools.partial(self._deliver_page, page, encoded))
                return

        try:
            raw_frame.runJavaScript(
                _TOKEN_JS, _APPLICATION_WORLD,
                functools.partial(self._deliver_frame, raw_frame, encoded))
        except (RuntimeError, AttributeError):
            log.extensions.debug("Could not query frame token")

    def _parse_token(self, result: Any) -> str | None:
        try:
            decoded = json.loads(result) if isinstance(result, str) else None
        except (ValueError, json.JSONDecodeError):
            return None
        if (isinstance(decoded, list) and len(decoded) == 2
                and isinstance(decoded[0], str)
                and _TOKEN_RE.fullmatch(decoded[0])):
            return decoded[0]
        return None

    def _deliver_frame(self, raw_frame: Any, encoded: str, result: Any) -> None:
        token = self._parse_token(result)
        if token is None or not self._frame_is_valid(raw_frame):
            return
        code = self._delivery_code(token, encoded)
        QTimer.singleShot(
            0, functools.partial(self._run_delivery, raw_frame, code))

    def _run_delivery(self, raw_frame: Any, code: str) -> None:
        try:
            raw_frame.runJavaScript(code, _APPLICATION_WORLD)
        except (RuntimeError, AttributeError):
            log.extensions.debug("Could not deliver extension message")

    def _deliver_page(self, page: Any, encoded: str, result: Any) -> None:
        token = self._parse_token(result)
        if token is None or sip.isdeleted(page):
            return
        code = self._delivery_code(token, encoded)
        page.runJavaScript(code, _APPLICATION_WORLD)

    @staticmethod
    def _delivery_code(token: str, encoded: str) -> str:
        return (
            "(function(){try{"
            "var m=window._qutebrowser.extensions.messaging;"
            f"if(!m||m.token!=={json.dumps(token)})return;"
            f"m._receive({encoded});"
            "}catch(e){}})();"
        )


def _shim_source() -> str:
    return javascript.wrap_global(
        'extension_messaging',
        resources.read_file('javascript/qwebchannel.js'),
        resources.read_file('javascript/extension-messaging.js'),
    )


def attach(tab: Any) -> MessageBridge | None:
    """Attach the messaging channel to a WebEngine tab."""
    if not has_handlers():
        return None
    if getattr(tab, '_messaging_bridge', None) is not None:
        return tab._messaging_bridge

    tab._messaging_supported = False
    bridge = MessageBridge(tab)
    channel = QWebChannel(tab)
    channel.registerObject('qutebrowser', bridge)
    page = tab._widget.page()
    page.setWebChannel(channel, _APPLICATION_WORLD)
    tab.add_web_script(
        _SHIM_NAME,
        _shim_source(),
        world=usertypes.JsWorld.application,
        injection_point=usertypes.InjectionPoint.creation,
        subframes=True,
    )
    tab._messaging_bridge = bridge
    tab._messaging_channel = channel
    tab._messaging_probe_done = False
    tab.load_finished.connect(functools.partial(_probe, tab))
    return bridge


def detach(tab: Any) -> None:
    bridge = getattr(tab, '_messaging_bridge', None)
    if bridge is not None:
        bridge.enabled = False
    tab._messaging_supported = False
    channel = getattr(tab, '_messaging_channel', None)
    if channel is not None:
        try:
            tab._widget.page().setWebChannel(None)
        except (RuntimeError, AttributeError):
            pass
        try:
            channel.deleteLater()
        except RuntimeError:
            pass
    try:
        tab.remove_web_script(_SHIM_NAME)
    except (RuntimeError, AttributeError):
        pass
    tab._messaging_bridge = None
    tab._messaging_channel = None


def _probe(tab: Any, ok: bool) -> None:
    if not ok:
        return
    bridge = getattr(tab, '_messaging_bridge', None)
    if bridge is None or bridge.enabled or getattr(
            tab, '_messaging_probe_done', False):
        return
    tab._messaging_probe_done = True
    page = tab._widget.page()
    state: dict[str, Any] = {}
    finished = False

    def finish() -> None:
        nonlocal finished
        if finished or not {'main', 'app'} <= state.keys():
            return
        finished = True
        if state['main'] is False and state['app'] is True:
            bridge.enabled = True
            tab._messaging_supported = True
            log.extensions.debug("Extension messaging enabled for tab")
        else:
            log.extensions.warning(
                "Extension messaging isolation probe failed; disabling")
            detach(tab)

    def timeout() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        log.extensions.warning(
            "Extension messaging isolation probe timed out; disabling")
        detach(tab)

    def on_main(result: Any) -> None:
        state['main'] = bool(result)
        finish()

    def on_app(result: Any) -> None:
        state['app'] = bool(result)
        finish()

    page.runJavaScript(
        "typeof qt !== 'undefined' && "
        "typeof qt.webChannelTransport !== 'undefined'", 0, on_main)
    page.runJavaScript(
        "typeof qt !== 'undefined' && "
        "typeof qt.webChannelTransport !== 'undefined'",
        _APPLICATION_WORLD, on_app)
    QTimer.singleShot(2000, timeout)
