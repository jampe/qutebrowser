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
from qutebrowser.utils import usertypes


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


def make_message(tab, frame, type_, payload, *, is_main=True):
    return messaging.Message(
        tab=tab, frame=frame, origin='https://example.com',
        relayed=False, is_main=is_main, channel='bitwarden',
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
        'form_id': None,
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


def test_bwserve_session_from_env(monkeypatch):
    monkeypatch.setenv('BW_SESSION', 'session-value')
    vault = bitwarden.BwServeVault()
    try:
        assert vault.unlocked
        assert vault._session == 'session-value'  # noqa: SLF001
    finally:
        vault.close()


def test_bwserve_list_items(monkeypatch):
    vault = bitwarden.BwServeVault()
    raw = {
        'id': '1',
        'name': 'Example',
        'login': {
            'username': 'alice',
            'password': 'secret',
            'totp': None,
            'uris': [{'uri': 'https://example.com/'}],
        },
    }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        return {'object': 'list', 'data': [raw]}

    monkeypatch.setattr(vault, '_request', request)
    items = vault.list_items('https://example.com/login')

    assert calls == [(
        'GET',
        '/list/object/items?url=https%3A%2F%2Fexample.com%2Flogin',
    )]
    assert [item.id for item in items] == ['1']
    assert items[0].username == 'alice'
    vault.close()


def test_bwserve_get_item_and_totp(monkeypatch):
    vault = bitwarden.BwServeVault()
    raw = {
        'id': '1',
        'name': 'Example',
        'login': {'username': 'alice', 'password': 'secret'},
    }

    def request(method, path, **kwargs):
        if path.endswith('/totp/1'):
            return {'object': 'string', 'data': '123456'}
        return raw

    monkeypatch.setattr(vault, '_request', request)
    assert vault.get_item('1').password == 'secret'
    assert vault.get_totp('1') == '123456'
    vault.close()


def test_bwserve_unlock_and_lock(monkeypatch):
    monkeypatch.delenv('BW_SESSION', raising=False)
    vault = bitwarden.BwServeVault()
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get('data')))
        return {'object': 'message', 'raw': 'new-session'}

    monkeypatch.setattr(vault, '_request', request)
    assert vault.unlock('master')
    assert vault.unlocked
    assert vault._session == 'new-session'  # noqa: SLF001

    vault.lock()
    assert not vault.unlocked
    assert calls == [('POST', '/unlock', {'password': 'master'})]
    vault.close()


def test_bwserve_invalid_session_clears(monkeypatch):
    monkeypatch.delenv('BW_SESSION', raising=False)
    vault = bitwarden.BwServeVault()
    vault._session = 'stale'  # noqa: SLF001
    vault._unlocked = True  # noqa: SLF001
    vault._port = 1  # noqa: SLF001
    monkeypatch.setattr(vault, '_ensure_started', lambda: None)

    class Response:
        status = 400

        def read(self):
            return b'{"success":false,"message":"Vault is locked."}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout=None):
            return Response()

    monkeypatch.setattr(vault, '_opener', Opener())
    with pytest.raises(bitwarden.VaultError, match='Vault is locked'):
        vault.list_items('example.com')
    assert not vault.unlocked
    assert vault._session is None  # noqa: SLF001
    vault.close()


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
    component._ask_master_password = lambda callback, **kwargs: callback('master')
    results = []
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert results == [(True, None)]
    assert component.vault.last_password == 'master'
    assert component.vault.unlocked


def test_master_password_uses_builtin_prompt(component, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, handler, **kwargs: (
            calls.append((title, mode)), handler('master')))
    results = []
    component._ask_master_password(results.append)
    assert calls == [('Bitwarden Master Password', usertypes.PromptMode.password)]
    assert results == ['master']


def test_unlock_cancel_suppresses_retry(component):
    component.vault.lock()
    prompts = []

    def ask(callback, **kwargs):
        prompts.append((callback, kwargs))

    component._ask_master_password = ask  # type: ignore[method-assign]
    results = []
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert len(prompts) == 1

    prompts[0][1]['cancelled']()
    assert results == [(False, 'Unlock cancelled')]

    # Automatic lookups must not reopen the prompt immediately.
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert len(prompts) == 1
    assert results[-1] == (False, 'Unlock cancelled')

    # An explicit unlock command may prompt again.
    component._ask_master_password = (
        lambda callback, **kwargs: callback('master'))  # type: ignore[method-assign]
    component.unlock_now(lambda ok, error: results.append((ok, error)))
    assert results[-1] == (True, None)


def test_unlock_prompt_is_deduplicated(component):
    component.vault.lock()
    prompts = []
    component._ask_master_password = (
        lambda callback, **kwargs: prompts.append(callback))  # type: ignore[method-assign]
    results = []
    component._ensure_unlocked(lambda ok, error: results.append(('a', ok)))
    component._ensure_unlocked(lambda ok, error: results.append(('b', ok)))
    assert len(prompts) == 1
    prompts[0]('master')
    assert results == [('a', True), ('b', True)]


def test_master_password_empty_cancels(component):
    component.vault.lock()
    component._ask_master_password = lambda callback, **kwargs: callback(None)
    results = []
    component._ensure_unlocked(lambda ok, error: results.append((ok, error)))
    assert results == [(False, 'Unlock cancelled')]


def test_filled_event_is_accepted(component, tab, frame):
    message = make_message(
        tab, frame, 'filled', {'auto': True})
    component.on_message(message)
    assert tab.sent == []


def test_generate_password_password_mode(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = 'generated-value\n'
        stderr = ''

    def run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(bitwarden.shutil, 'which', lambda _name: '/bin/bw')
    monkeypatch.setattr(bitwarden.subprocess, 'run', run)
    result = bitwarden.generate_password({
        'mode': 'password',
        'length': 20,
        'uppercase': True,
        'lowercase': True,
        'number': False,
        'special': True,
        'minNumber': 0,
        'minSpecial': 2,
        'ambiguous': True,
    })
    assert result == 'generated-value'
    assert calls == [[
        '/bin/bw', 'generate', '-u', '-l', '-s', '--length', '20',
        '--minSpecial', '2', '--ambiguous',
    ]]


def test_generate_password_passphrase_mode(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = 'correct-horse\n'
        stderr = ''

    def run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(bitwarden.shutil, 'which', lambda _name: '/bin/bw')
    monkeypatch.setattr(bitwarden.subprocess, 'run', run)
    result = bitwarden.generate_password({
        'mode': 'passphrase',
        'words': 4,
        'separator': 'space',
        'capitalize': True,
        'includeNumber': True,
    })
    assert result == 'correct-horse'
    assert calls == [[
        '/bin/bw', 'generate', '-p', '--words', '4',
        '--separator=space', '-c', '--includeNumber',
    ]]


def test_generate_password_rejects_empty_character_sets(monkeypatch):
    monkeypatch.setattr(bitwarden.shutil, 'which', lambda _name: '/bin/bw')
    with pytest.raises(bitwarden.VaultError, match='character set'):
        bitwarden.generate_password({
            'mode': 'password',
            'length': 14,
            'uppercase': False,
            'lowercase': False,
            'number': False,
            'special': False,
        })


def test_bwserve_create_and_update_login(monkeypatch):
    vault = bitwarden.BwServeVault()
    raw = {
        'id': '1',
        'name': 'Example',
        'notes': 'keep me',
        'fields': [{'name': 'custom', 'value': 'value'}],
        'login': {
            'username': 'alice',
            'password': 'old',
            'uris': [{'uri': 'https://old.example/'}],
        },
    }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get('data')))
        if method == 'GET':
            return raw
        if method == 'POST':
            return {
                'id': '2', 'name': 'example.com',
                'login': kwargs['data']['login'],
            }
        if method == 'PUT':
            return kwargs['data']
        raise AssertionError(method)

    monkeypatch.setattr(vault, '_request', request)
    created = vault.create_login(
        'https://example.com/login', 'alice', 'new-secret')
    assert created.username == 'alice'
    assert created.uris == ['https://example.com/']
    post = calls[0]
    assert post[0] == 'POST'
    assert post[1] == '/object/item'
    assert post[2]['login']['uris'] == [{'uri': 'https://example.com/'}]

    updated = vault.update_login(
        '1', username='alice', password='new-secret',
        url='https://example.com/login')
    assert updated.password == 'new-secret'
    get_call, put_call = calls[1], calls[2]
    assert get_call[:2] == ('GET', '/object/item/1')
    assert put_call[:2] == ('PUT', '/object/item/1')
    body = put_call[2]
    assert body['notes'] == 'keep me'
    assert body['fields'] == [{'name': 'custom', 'value': 'value'}]
    assert body['login']['password'] == 'new-secret'
    assert {'uri': 'https://example.com/'} in body['login']['uris']
    vault.close()


def test_normalize_item_reads_nested_login_uris():
    item = bitwarden._normalize_item({  # noqa: SLF001
        'id': '1', 'name': 'Example',
        'uris': [{'uri': 'https://old.example/'}],
        'login': {
            'username': 'alice', 'password': 'secret',
            'uris': [{'uri': 'https://example.com/'}],
        },
    })
    assert item is not None
    assert item.uris == ['https://example.com/']


def test_fake_vault_create_update():
    vault = bitwarden.FakeVault([])
    item = vault.create_login('https://example.com/login', 'alice', 'secret')
    assert vault.list_items('https://example.com/') == [item]
    updated = vault.update_login(
        item.id, username='alice', password='new', url='https://example.com/')
    assert updated.password == 'new'
    assert updated.name == item.name
    assert updated.uris == item.uris


def _save_message(tab, frame, **overrides):
    payload = {
        'url': 'https://example.com/login',
        'username': 'alice',
        'password': 'secret',
        'registration': False,
        'registration_strong': False,
        'password_change': False,
        'form_id': 'form-1',
        'form_action': '/login',
    }
    payload.update(overrides)
    return make_message(tab, frame, 'saveCandidate', payload)


def test_save_prompt_creates_login(component, tab, frame, monkeypatch):
    component._vault = bitwarden.FakeVault([])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, callback, **kwargs: prompts.append(
            (title, callback, kwargs)))
    monkeypatch.setattr(bitwarden.message, 'info', lambda _msg: None)
    message = _save_message(tab, frame)
    component.on_message(message)
    assert len(prompts) == 1
    assert 'Save login' in prompts[0][0]
    prompts[0][1](True)
    assert component._vault.list_items('https://example.com/')[0].password == 'secret'
    assert not component._save_prompt_active


def test_save_prompt_updates_existing(component, tab, frame, monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, callback, **kwargs: prompts.append(
            (title, callback, kwargs)))
    monkeypatch.setattr(bitwarden.message, 'info', lambda _msg: None)
    message = _save_message(
        tab, frame, password='new-secret', form_id='form-2')
    component.on_message(message)
    assert 'Update password' in prompts[0][0]
    prompts[0][1](True)
    assert component._vault.get_item('1').password == 'new-secret'


def test_save_same_password_does_not_prompt(component, tab, frame,
                                            monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda *args, **kwargs: prompts.append(args))
    component.on_message(_save_message(
        tab, frame, password='secret', form_id='form-3'))
    assert prompts == []
    assert not component._save_prompt_active


def test_save_registration_existing_username_skips_update(
        component, tab, frame, monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda *args, **kwargs: prompts.append(args))
    component.on_message(_save_message(
        tab, frame, password='different', registration=True,
        registration_strong=True, form_id='form-4'))
    assert prompts == []
    assert component._vault.get_item('1').password == 'secret'


def test_save_weak_registration_skips(component, tab, frame, monkeypatch):
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda *args, **kwargs: prompts.append(args))
    component.on_message(_save_message(
        tab, frame, registration=True, registration_strong=False,
        form_id='form-5'))
    assert prompts == []
    assert not component._save_prompt_active


def test_save_multiple_username_matches_skip(component, tab, frame,
                                             monkeypatch):
    duplicate = bitwarden.VaultItem(
        id='2', name='Other', username='alice', password='other',
        uris=['https://example.com/'])
    component._vault = bitwarden.FakeVault([ITEMS[0], duplicate])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda *args, **kwargs: prompts.append(args))
    component.on_message(_save_message(
        tab, frame, password='different', form_id='form-6'))
    assert prompts == []
    assert not component._save_prompt_active


def test_generate_message_returns_password(component, tab, frame, monkeypatch):
    monkeypatch.setattr(bitwarden, 'generate_password',
                        lambda options: 'generated')
    message = make_message(
        tab, frame, 'generatePassword',
        {'request_id': 7, 'form_id': 'form-1',
         'options': {'mode': 'password'}})
    component.on_message(message)
    envelope, target = tab.sent[0]
    assert target is frame
    assert envelope['payload'] == {
        'type': 'generatedPassword',
        'request_id': 7,
        'form_id': 'form-1',
        'password': 'generated',
    }


def test_save_password_change_updates_existing(component, tab, frame,
                                               monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, callback, **kwargs: prompts.append(
            (title, callback, kwargs)))
    monkeypatch.setattr(bitwarden.message, 'info', lambda _msg: None)
    message = _save_message(
        tab, frame, password='changed', registration=True,
        registration_strong=True, password_change=True, form_id='form-7')
    component.on_message(message)
    assert 'Update password' in prompts[0][0]
    prompts[0][1](True)
    assert component._vault.get_item('1').password == 'changed'


def test_fake_vault_search_and_uri_only_update():
    vault = bitwarden.FakeVault([ITEMS[0]])
    assert vault.search_items('example') == [ITEMS[0]]
    assert vault.search_items('alice') == [ITEMS[0]]
    assert vault.search_items('missing') == []
    assert vault.search_items('  ') == []
    updated = vault.update_login(
        '1', url='https://other.example/login', add_uri=True)
    assert updated.password == 'secret'
    assert updated.username == 'alice'
    assert 'https://other.example/' in updated.uris


def test_bwserve_search_items(monkeypatch):
    vault = bitwarden.BwServeVault()
    raw = {
        'id': '1', 'name': 'Example',
        'login': {
            'username': 'alice', 'password': 'secret',
            'uris': [{'uri': 'https://example.com/'}],
        },
    }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        return {'object': 'list', 'data': [raw]}

    monkeypatch.setattr(vault, '_request', request)
    items = vault.search_items('example login')
    assert [item.id for item in items] == ['1']
    assert calls == [(
        'GET',
        '/list/object/items?search=example+login',
    )]
    assert vault.search_items('   ') == []
    vault.close()


def test_candidates_error_still_opens_menu(component, tab, frame,
                                           monkeypatch):
    monkeypatch.setattr(bitwarden.log.extensions, 'warning', lambda *a: None)
    component._send_candidates(
        make_message(tab, frame, 'fieldFocus', {}), [],
        'Vault is locked.',
        form_id='form-1', document_id='doc-1')
    envelope, target = tab.sent[-1]
    assert target is frame
    assert envelope['payload'] == {
        'type': 'candidates',
        'items': [],
        'form_id': 'form-1',
        'document_id': 'doc-1',
        'error': 'Vault is locked.',
    }


def test_search_is_main_frame_only(component, tab, frame):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    payload = {
        'request_id': 3, 'document_id': 'doc-1',
        'form_id': 'form-1', 'query': 'alice',
    }
    component.on_message(make_message(
        tab, frame, 'searchItems', payload, is_main=False))
    assert tab.sent == []

    component.on_message(make_message(
        tab, frame, 'searchItems', payload))
    envelope, target = tab.sent[-1]
    assert target is frame
    assert envelope['type'] == 'searchResults'
    assert envelope['payload']['request_id'] == 3
    assert envelope['payload']['items'][0]['username'] == 'alice'
    assert 'password' not in envelope['payload']['items'][0]


def test_selected_fill_and_add_uri(component, tab, frame, monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, callback, **kwargs: prompts.append(
            (title, callback, kwargs)))
    monkeypatch.setattr(bitwarden.message, 'info', lambda _msg: None)

    fill = make_message(tab, frame, 'fill', {
        'item_id': '1',
        'document_id': 'doc-1',
        'form_id': 'form-1',
        'source': 'search',
        'url': 'https://other.example/login',
    })
    component.on_message(fill)
    assert len(component._selected) == 1

    save = _save_message(
        tab, frame, url='https://other.example/login',
        password='secret', document_id='doc-1', form_id='form-1')
    component.on_message(save)
    assert len(prompts) == 1
    assert prompts[0][0].startswith('Add other.example')
    prompts[0][1](True)
    item = component._vault.get_item('1')
    assert 'https://other.example/' in item.uris
    assert item.password == 'secret'


def test_route_changed_clears_selection(component, tab, frame):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    component.on_message(make_message(tab, frame, 'fill', {
        'item_id': '1', 'document_id': 'doc-1', 'form_id': 'form-1',
        'source': 'search', 'url': 'https://other.example/login',
    }))
    assert component._selected
    component.on_message(make_message(tab, frame, 'routeChanged', {
        'document_id': 'doc-1', 'url': 'https://other.example/next',
    }))
    assert component._selected == {}


def test_bwserve_add_uri_only_preserves_credentials(monkeypatch):
    vault = bitwarden.BwServeVault()
    raw = {
        'id': '1', 'name': 'Example',
        'login': {
            'username': 'alice', 'password': 'secret',
            'uris': [{'uri': 'https://old.example/'}],
        },
    }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get('data')))
        if method == 'GET':
            return raw
        return kwargs['data']

    monkeypatch.setattr(vault, '_request', request)
    item = vault.update_login(
        '1', url='https://new.example/login', add_uri=True)
    assert item.username == 'alice'
    assert item.password == 'secret'
    assert 'https://new.example/' in item.uris
    assert calls[1][2]['login']['username'] == 'alice'
    assert calls[1][2]['login']['password'] == 'secret'
    vault.close()


def test_selected_fill_password_change_updates_and_adds_uri(
        component, tab, frame, monkeypatch):
    component._vault = bitwarden.FakeVault([ITEMS[0]])
    prompts = []
    monkeypatch.setattr(
        bitwarden.qmessage, 'ask_async',
        lambda title, mode, callback, **kwargs: prompts.append(
            (title, callback, kwargs)))
    monkeypatch.setattr(bitwarden.message, 'info', lambda _msg: None)
    component.on_message(make_message(tab, frame, 'fill', {
        'item_id': '1', 'document_id': 'doc-1', 'form_id': 'form-1',
        'source': 'search', 'url': 'https://other.example/login',
    }))
    component.on_message(_save_message(
        tab, frame, url='https://other.example/login',
        password='changed', document_id='doc-1', form_id='form-1'))
    assert 'Update password and add' in prompts[0][0]
    prompts[0][1](True)
    item = component._vault.get_item('1')
    assert item.password == 'changed'
    assert 'https://other.example/' in item.uris
