# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for extension messaging."""

import json
import logging
from typing import Any

import pytest

from qutebrowser.qt.core import QObject, QUrl

from qutebrowser.api import apitypes
from qutebrowser.extensions import messaging


TOKEN = '0123456789abcdef0123456789abcdef'


class FakeRawFrame:

    def __init__(self, url: str, token: str = TOKEN,
                 origin: str = 'https://example.com',
                 valid: bool = True, is_main: bool = True) -> None:
        self._url = QUrl(url)
        self.token = token
        self.origin = origin
        self.valid = valid
        self._is_main = is_main
        self.scripts: list[tuple[str, int, Any]] = []

    def url(self) -> QUrl:
        return self._url

    def isValid(self) -> bool:
        return self.valid

    def isMainFrame(self) -> bool:
        return self._is_main

    def runJavaScript(self, script: str, world: int, callback: Any = None) -> None:
        self.scripts.append((script, world, callback))
        if callback is not None and script == messaging._TOKEN_JS:
            callback(json.dumps([self.token, self.origin]))


class FakeTab(QObject):

    def __init__(self, frame: FakeRawFrame) -> None:
        super().__init__()
        self.frame = frame
        self.messaging_supported = True
        self.site_isolation = False
        self.can_fill_cross_site = False
        self.sent: list[tuple[Any, Any]] = []
        self.adapter = apitypes.ExtensionTab(self)

    def url(self) -> QUrl:
        return QUrl('https://example.com/')

    def frames(self) -> list[FakeRawFrame]:
        return [self.frame]

    def api_adapter(self) -> apitypes.ExtensionTab:
        return self.adapter

    def send_message(self, payload: Any, *, frame: Any = None) -> None:
        self.sent.append((payload, frame))

    def data(self) -> Any:
        raise AssertionError


@pytest.fixture(autouse=True)
def clean_handlers():
    messaging._handlers.clear()
    yield
    messaging._handlers.clear()


@pytest.fixture
def frame() -> FakeRawFrame:
    return FakeRawFrame('https://example.com/login')


@pytest.fixture
def tab(frame, qapp) -> FakeTab:
    return FakeTab(frame)


@pytest.fixture
def bridge(tab) -> messaging.MessageBridge:
    bridge = messaging.MessageBridge(tab)
    bridge.enabled = True
    return bridge


def make_message(token=TOKEN, channel='test', origin='https://example.com',
                 relayed=False, payload=None):
    return json.dumps({
        'token': token,
        'channel': channel,
        'type': 'event',
        'request_id': '1',
        'origin': origin,
        'relayed': relayed,
        'payload': payload or {'hello': 'world'},
    })


def test_register_duplicate_and_reserved(caplog):
    calls = []
    messaging.register('test', lambda message: calls.append(message))
    with caplog.at_level(logging.ERROR, 'extensions'):
        messaging.register('test', lambda message: calls.append(message))
        messaging.register('qute_reserved', lambda message: calls.append(message))
    assert list(messaging._handlers) == ['test']


def test_duplicate_registration_rejected(caplog):
    messaging.register('test', lambda message: None)
    with caplog.at_level(logging.ERROR, 'extensions'):
        messaging.register('test', lambda message: None)
    assert list(messaging._handlers) == ['test']


def test_drops_while_disabled(tab, qapp):
    bridge = messaging.MessageBridge(tab)
    calls = []
    messaging.register('test', calls.append)
    bridge.send(make_message())
    assert calls == []


def test_dispatch_after_token_resolution(tab, bridge, qtbot):
    calls = []
    messaging.register('test', calls.append)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'

    bridge.send(make_message())
    qtbot.waitUntil(lambda: bool(calls), timeout=1000)

    message = calls[0]
    assert message.channel == 'test'
    assert message.type == 'event'
    assert message.origin == 'https://example.com'
    assert message.is_main
    assert not message.relayed
    assert message.payload == {'hello': 'world'}
    assert message.frame.tab is tab.adapter


def test_relay_requires_opt_in(tab, bridge, qtbot):
    calls = []
    messaging.register('test', calls.append, allow_relay=False)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'
    bridge.send(make_message(relayed=True))
    qtbot.wait(50)
    assert calls == []


def test_relay_allowed(tab, bridge, qtbot):
    calls = []
    messaging.register('test', calls.append, allow_relay=True)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'
    bridge.send(make_message(relayed=True))
    qtbot.waitUntil(lambda: bool(calls), timeout=1000)
    assert calls[0].relayed


def test_origin_mismatch_dropped(tab, bridge, qtbot):
    calls = []
    messaging.register('test', calls.append, allow_relay=True)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'
    bridge.send(make_message(origin='https://evil.example'))
    qtbot.wait(50)
    assert calls == []


def test_opaque_origin_dropped(tab, bridge, qtbot):
    calls = []
    messaging.register('test', calls.append, allow_relay=True)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = None
    bridge.send(make_message(origin='null'))
    qtbot.wait(50)
    assert calls == []


@pytest.mark.parametrize('payload', [
    'not json',
    json.dumps({'token': 'short'}),
    json.dumps({'token': TOKEN, 'channel': 'test', 'type': 'x',
                'origin': 'https://example.com', 'payload': []}),
    json.dumps({'token': TOKEN, 'channel': 'test', 'type': 'x',
                'origin': 'https://example.com', 'payload': {},
                'relayed': 'yes'}),
])
def test_invalid_messages_dropped(tab, bridge, payload, caplog):
    calls = []
    messaging.register('test', calls.append, allow_relay=True)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'
    with caplog.at_level(logging.WARNING, 'extensions'):
        bridge.send(payload)
    assert calls == []


def test_oversized_message_dropped(tab, bridge, caplog):
    calls = []
    messaging.register('test', calls.append, allow_relay=True)
    bridge._token_to_frame[TOKEN] = tab.frame
    bridge._frame_origin[id(tab.frame)] = 'https://example.com'
    with caplog.at_level(logging.WARNING, 'extensions'):
        bridge.send(make_message(payload={'data': 'x' * (64 * 1024)}))
    assert calls == []


def test_post_queries_token_and_delivers(tab, bridge, qtbot):
    payload = {'channel': 'test', 'type': 'fill', 'payload': {'x': 1}}
    bridge.post(payload, tab.frame)
    qtbot.wait(10)
    assert len(tab.frame.scripts) == 2
    token_script, world, _callback = tab.frame.scripts[0]
    assert token_script == messaging._TOKEN_JS
    assert world == messaging._APPLICATION_WORLD
    delivery_script, delivery_world, delivery_callback = tab.frame.scripts[1]
    assert delivery_world == messaging._APPLICATION_WORLD
    assert delivery_callback is None
    assert TOKEN in delivery_script
    assert '_receive' in delivery_script
    assert 'fill' in delivery_script


def test_post_requires_enabled(tab):
    bridge = messaging.MessageBridge(tab)
    with pytest.raises(Exception, match='not available'):
        bridge.post({'channel': 'test'})


def test_adapter_send_message_main_frame(tab):
    tab.adapter.send_message({'channel': 'test'}, frame=None)
    assert tab.sent == [({'channel': 'test'}, None)]


def test_adapter_send_message_cross_site_refused(tab):
    other = FakeRawFrame('https://evil.example/login', is_main=False)
    frame = apitypes.Frame(tab.adapter, other)
    with pytest.raises(Exception, match='cross-site'):
        tab.adapter.send_message({'channel': 'test'}, frame=frame)


def test_adapter_send_message_same_site(tab):
    sub = FakeRawFrame('https://login.example.com/', is_main=False)
    frame = apitypes.Frame(tab.adapter, sub)
    tab.adapter.send_message({'channel': 'test'}, frame=frame)
    assert tab.sent == [({'channel': 'test'}, sub)]


def test_adapter_send_message_requires_backend_support(tab):
    tab.messaging_supported = False
    with pytest.raises(Exception, match='not available'):
        tab.adapter.send_message({'channel': 'test'})
