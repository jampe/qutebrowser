# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the Bitwarden component page integration."""

import json
from typing import Any

import pytest

from qutebrowser.qt.core import QUrl

from qutebrowser.components import bitwarden
from qutebrowser.extensions import messaging


ITEMS = [
    bitwarden.VaultItem(
        id='1', name='Example', username='alice', password='secret',
        totp='123456', uris=['https://example.com/']),
    bitwarden.VaultItem(
        id='2', name='Other', username='bob', password='other',
        uris=['https://example.com/']),
]


class FakeFrame:

    def __init__(self, url: str, *, is_main: bool = True) -> None:
        self.url = QUrl(url)
        self.is_main = is_main
        self.is_valid = True


class FakeTab:

    def __init__(self, url: str = 'https://example.com/login') -> None:
        self._url = QUrl(url)
        self.is_private = False
        self.messaging_supported = True
        self.sent: list[tuple[dict[str, Any], Any]] = []

    def url(self) -> QUrl:
        return self._url

    def send_message(self, payload: dict[str, Any], *,
                     frame: Any = None) -> None:
        self.sent.append((payload, frame))


@pytest.fixture
def component(qapp) -> bitwarden.BitwardenComponent:
    component = bitwarden.BitwardenComponent(
        vault=bitwarden.FakeVault(ITEMS))
    # Make vault calls synchronous for deterministic assertions.
    component._run = lambda func, callback: callback(func(), None)  # type: ignore[method-assign]
    return component


@pytest.fixture
def tab() -> FakeTab:
    return FakeTab()


@pytest.fixture
def frame() -> FakeFrame:
    return FakeFrame('https://example.com/login')


def make_message(tab, frame, type_, payload):
    return messaging.Message(
        tab=tab, frame=frame, origin='https://example.com',
        relayed=False, is_main=True, channel='bitwarden',
        type=type_, request_id='1', payload=payload)


def test_candidates_contain_no_secrets(component, tab, frame):
    message = make_message(
        tab, frame, 'fieldFocus',
        {'url': 'https://example.com/login', 'kind': 'username'})
    component.on_message(message)

    assert len(tab.sent) == 1
    envelope, target = tab.sent[0]
    payload = envelope['payload']
    assert target is frame
    assert envelope['channel'] == 'bitwarden'
    assert payload['type'] == 'candidates'
    assert [item['username'] for item in payload['items']] == [
        'alice', 'bob']
    assert all('password' not in item for item in payload['items'])
    assert all('totp' not in item for item in payload['items'])


def test_fill_sends_credentials_to_frame(component, tab, frame):
    message = make_message(
        tab, frame, 'fill', {'item_id': '1'})
    component.on_message(message)

    envelope, target = tab.sent[0]
    payload = envelope['payload']
    assert target is frame
    assert payload == {
        'type': 'credentials',
        'auto': False,
        'username': 'alice',
        'password': 'secret',
        'totp': '123456',
    }


def test_auto_fill_single_match(component, tab, frame):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    message = make_message(
        tab, frame, 'autoFillRequest',
        {'url': 'https://example.com/login'})
    component.on_message(message)

    assert len(tab.sent) == 1
    envelope, target = tab.sent[0]
    payload = envelope['payload']
    assert target is frame
    assert payload['auto'] is True
    assert payload['username'] == 'alice'


def test_auto_fill_multiple_matches_does_nothing(component, tab):
    component._vault = bitwarden.FakeVault([
        ITEMS[0],
        bitwarden.VaultItem(id='3', name='Third', username='carol',
                            password='third', uris=['https://example.com/']),
    ])
    message = make_message(
        tab, FakeFrame('https://example.com/login'),
        'autoFillRequest', {'url': 'https://example.com/login'})
    component.on_message(message)
    assert tab.sent == []


def test_python_driven_load_autofill(component, tab):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    payload = json.dumps({
        'url': 'https://example.com/login',
        'username': True,
        'password': True,
        'totp': False,
    })
    component._on_collected_fields(tab, payload)

    assert len(tab.sent) == 1
    envelope, target = tab.sent[0]
    payload = envelope['payload']
    assert target is None
    assert payload['auto'] is True
    assert payload['username'] == 'alice'


def test_auto_fill_toggle(component, tab, frame):
    component._autofill_on_load = False
    message = make_message(
        tab, frame, 'autoFillRequest',
        {'url': 'https://example.com/login'})
    component.on_message(message)
    assert tab.sent == []

    assert component.toggle_autofill() is True


def test_fake_vault_matches_subdomains():
    vault = bitwarden.FakeVault(ITEMS)
    assert [item.id for item in vault.list_items(
        'https://login.example.com/')] == ['1', '2']
    assert vault.list_items('https://elsewhere.invalid/') == []


def test_status(component):
    assert 'unlocked' in component.status()
    assert 'FakeVault' in component.status()


def test_unlock_uses_password_only(component):
    component.vault.lock()
    component._ask_master_password = lambda callback: callback('master')
    results = []
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert results == [(True, None)]
    assert component.vault.last_password == 'master'
    assert component.vault.unlocked


def test_master_password_empty_cancels(component):
    component.vault.lock()
    component._ask_master_password = lambda callback: callback(None)
    results = []
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert results == [(False, 'Unlock cancelled')]


def test_filled_event_is_accepted(component, tab, frame):
    message = make_message(
        tab, frame, 'filled', {'auto': True})
    component.on_message(message)
    assert tab.sent == []
