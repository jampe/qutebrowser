# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Content-script tests for the Bitwarden form/role/save logic."""

import json
import pathlib

import qutebrowser
from qutebrowser.qt.core import QUrl
from qutebrowser.utils import usertypes


STUB = """
window.__qbMsgs = [];
window._qutebrowser = {
    extensions: {
        messaging: {
            send: function (message) {
                window.__qbMsgs.push(message);
            },
            onMessage: function (channel, handler) {
                window.__qbHandler = handler;
            }
        }
    }
};
"""

CONTENT = (
    pathlib.Path(qutebrowser.__file__).resolve().parent /
    'javascript' / 'bitwarden' / 'content.js'
).read_text(encoding='utf-8')


def _js(tester, source, world=usertypes.JsWorld.application):
    with tester.qtbot.wait_callback() as callback:
        tester.tab.run_js_async(source, callback, world=world)
    return callback.args[0]


def _json(tester, source, world=usertypes.JsWorld.application):
    value = _js(tester, source, world=world)
    return json.loads(value) if value is not None else None


def _setup(tester, page='bitwarden_forms.html'):
    tester.load(page, base_url=QUrl('https://example.com/login'))
    tester.run(STUB, world=usertypes.JsWorld.application)
    tester.run(CONTENT, world=usertypes.JsWorld.application)
    tester.qtbot.wait(100)


def _forms(tester):
    fields = _json(tester, "JSON.stringify(window.__bwCollectFields())")
    return {form['action']: form for form in fields['forms']}


def _roles(tester, form_id):
    return _json(
        tester,
        "JSON.stringify(window.__bwPasswordRoles({!r}))".format(form_id))


def test_password_roles_are_conservative(qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    forms = _forms(js_tester_webengine)

    login = forms['/login']
    assert login['registration'] is False
    assert _roles(js_tester_webengine, login['id']) == [
        {'id': 'login-password', 'name': 'password', 'role': 'current'},
    ]

    register = forms['/register']
    assert register['registration_strong'] is True
    assert [role['role'] for role in _roles(
        js_tester_webengine, register['id'])] == ['new', 'confirm']

    change = forms['/change']
    assert change['password_change'] is True
    assert [role['role'] for role in _roles(
        js_tester_webengine, change['id'])] == [
        'current', 'new', 'confirm']
    # The disabled email is captured for save, not for menu interaction.
    assert change['username'] is False

    unmarked = forms['/unmarked']
    assert [role['role'] for role in _roles(
        js_tester_webengine, unmarked['id'])] == ['current', 'new']

    explicit = forms['/explicit']
    assert [role['role'] for role in _roles(
        js_tester_webengine, explicit['id'])] == ['new', 'confirm']

    sibling = forms['/confirm-sibling']
    assert [role['role'] for role in _roles(
        js_tester_webengine, sibling['id'])] == ['new', 'confirm']

    text_only = forms['/signup']
    assert text_only['registration'] is True
    assert text_only['registration_strong'] is False
    assert [role['role'] for role in _roles(
        js_tester_webengine, text_only['id'])] == ['unknown']


def test_hybrid_page_selects_login_form(qtbot, js_tester_webengine):
    _setup(js_tester_webengine, 'bitwarden_hybrid.html')
    fields = _json(js_tester_webengine,
                   "JSON.stringify(window.__bwCollectFields())")
    forms = {form['action']: form for form in fields['forms']}
    assert fields['form_id'] == forms['/login']['id']
    assert fields['registration'] is False

    _js(js_tester_webengine, "window.__bwRequestAutoFill();")
    messages = _json(js_tester_webengine,
                     "JSON.stringify(window.__qbMsgs)")
    auto = [message for message in messages
            if message.get('type') == 'autoFillRequest']
    assert auto
    assert auto[-1]['payload']['form_id'] == forms['/login']['id']


def test_fill_is_form_scoped_and_totp_is_global(qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    forms = _forms(js_tester_webengine)
    login_id = forms['/login']['id']

    _js(js_tester_webengine, """
        window.__bwFill({
            form_id: %r, username: 'alice', password: 'secret',
            totp: '123456', auto: false
        });
    """ % login_id)
    values = _json(js_tester_webengine, """JSON.stringify({
        loginUser: document.getElementById('login-user').value,
        loginPassword: document.getElementById('login-password').value,
        registerUser: document.getElementById('register-email').value,
        registerPassword: document.getElementById('register-new').value,
        otp: document.getElementById('outside-otp').value
    })""", world=usertypes.JsWorld.main)
    filled = _json(
        js_tester_webengine,
        "JSON.stringify(window.__quteBitwarden.filledForms)")
    assert values['loginUser'] == 'alice'
    assert values['loginPassword'] == 'secret'
    assert values['registerUser'] == ''
    assert values['registerPassword'] == ''
    assert values['otp'] == '123456'
    assert filled[login_id] is True

    reports = _json(js_tester_webengine,
                    "JSON.stringify(window.__qbMsgs)")
    filled = [m for m in reports if m.get('type') == 'filled']
    assert filled[-1]['payload']['form_id'] == login_id


def test_credentials_use_form_id(qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    forms = _forms(js_tester_webengine)
    login_id = forms['/login']['id']
    _js(js_tester_webengine, """
        window.__qbHandler({
            type: 'credentials', form_id: %r, auto: false,
            username: 'alice', password: 'secret', totp: '123456'
        });
    """ % login_id)
    values = _json(js_tester_webengine, """JSON.stringify({
        loginUser: document.getElementById('login-user').value,
        loginPassword: document.getElementById('login-password').value,
        registerUser: document.getElementById('register-email').value,
        registerPassword: document.getElementById('register-new').value,
        otp: document.getElementById('outside-otp').value
    })""", world=usertypes.JsWorld.main)
    assert values == {
        'loginUser': 'alice',
        'loginPassword': 'secret',
        'registerUser': '',
        'registerPassword': '',
        'otp': '123456',
    }


def test_prevented_submit_produces_no_candidate(qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    _js(js_tester_webengine, """
        const form = document.getElementById('register-form');
        form.addEventListener('submit', event => event.preventDefault(),
                               {once: true});
        form.dispatchEvent(new Event('submit', {bubbles: true,
                                                cancelable: true}));
    """, world=usertypes.JsWorld.main)
    js_tester_webengine.qtbot.wait(30)
    messages = _json(js_tester_webengine,
                     "JSON.stringify(window.__qbMsgs)")
    assert not [m for m in messages if m.get('type') == 'saveCandidate']


def test_submit_candidate_carries_registration_metadata(
        qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    forms = _forms(js_tester_webengine)
    register_id = forms['/register']['id']
    _js(js_tester_webengine, """(function () {
        document.getElementById('register-email').value = 'a@example.com';
        document.getElementById('register-new').value = 'generated';
        document.getElementById('register-confirm').value = 'generated';
        const form = document.getElementById('register-form');
        form.dispatchEvent(new Event('submit', {bubbles: true,
                                                cancelable: true}));
    })();""", world=usertypes.JsWorld.main)
    js_tester_webengine.qtbot.wait(30)
    messages = _json(js_tester_webengine,
                     "JSON.stringify(window.__qbMsgs)")
    candidates = [m for m in messages if m.get('type') == 'saveCandidate']
    assert len(candidates) == 1
    payload = candidates[0]['payload']
    assert payload['form_id'] == register_id
    assert payload['registration'] is True
    assert payload['registration_strong'] is True
    assert payload['password_change'] is False
    assert payload['username'] == 'a@example.com'
    assert payload['password'] == 'generated'
    assert 'totp' not in payload


def test_generator_button_appears_only_on_registration(
        qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    count = _js(js_tester_webengine,
                "document.querySelectorAll("
                "'[data-qute-bitwarden-generate]').length",
                world=usertypes.JsWorld.main)
    assert count >= 1

    _setup(js_tester_webengine, 'bitwarden_login.html')
    count = _js(js_tester_webengine,
                "document.querySelectorAll("
                "'[data-qute-bitwarden-generate]').length",
                world=usertypes.JsWorld.main)
    assert count == 0


def test_formless_login_uses_document_form(qtbot, js_tester_webengine):
    _setup(js_tester_webengine, 'bitwarden_formless.html')
    fields = _json(js_tester_webengine,
                   "JSON.stringify(window.__bwCollectFields())")
    assert fields['form_id'] == '__document__'
    assert fields['username'] is True
    assert fields['password'] is True

    _js(js_tester_webengine, """
        window.__bwFill({
            form_id: '__document__', username: 'alice', password: 'secret',
            auto: false
        });
    """)
    values = _json(js_tester_webengine, """JSON.stringify({
        user: document.getElementById('fl-user').value,
        password: document.getElementById('fl-password').value
    })""", world=usertypes.JsWorld.main)
    assert values == {'user': 'alice', 'password': 'secret'}


def test_shadow_form_submit_is_resolved(qtbot, js_tester_webengine):
    _setup(js_tester_webengine, 'bitwarden_shadow.html')
    _js(js_tester_webengine, """(function () {
        const root = document.getElementById('shadow-host').shadowRoot;
        root.getElementById('shadow-user').value = 'alice';
        root.getElementById('shadow-password').value = 'secret';
        root.getElementById('shadow-login').dispatchEvent(new Event(
            'submit', {bubbles: true, cancelable: true, composed: true}));
    })();""", world=usertypes.JsWorld.main)
    js_tester_webengine.qtbot.wait(30)
    messages = _json(js_tester_webengine,
                     "JSON.stringify(window.__qbMsgs)")
    candidates = [m for m in messages if m.get('type') == 'saveCandidate']
    assert len(candidates) == 1
    assert candidates[0]['payload']['username'] == 'alice'
    assert candidates[0]['payload']['password'] == 'secret'


def test_change_submit_marks_password_change(qtbot, js_tester_webengine):
    _setup(js_tester_webengine)
    _js(js_tester_webengine, """(function () {
        document.getElementById('change-email').value = 'a@example.com';
        document.getElementById('change-current').value = 'old-secret';
        document.getElementById('change-new').value = 'new-secret';
        document.getElementById('change-confirm').value = 'new-secret';
        document.getElementById('change-form').dispatchEvent(new Event(
            'submit', {bubbles: true, cancelable: true}));
    })();""", world=usertypes.JsWorld.main)
    js_tester_webengine.qtbot.wait(30)
    messages = _json(js_tester_webengine,
                     "JSON.stringify(window.__qbMsgs)")
    candidates = [m for m in messages if m.get('type') == 'saveCandidate']
    assert len(candidates) == 1
    payload = candidates[0]['payload']
    assert payload['registration'] is True
    assert payload['registration_strong'] is True
    assert payload['password_change'] is True


def test_lookup_error_renders_search_menu(qtbot, js_tester_webengine):
    _setup(js_tester_webengine, 'bitwarden_login.html')
    fields = _json(js_tester_webengine,
                   "JSON.stringify(window.__bwCollectFields())")
    _js(js_tester_webengine,
        "document.getElementById('login-user').dispatchEvent("
        "new FocusEvent('focusin', {bubbles: true}))",
        world=usertypes.JsWorld.main)
    qtbot.wait(30)
    _js(js_tester_webengine, """
        window.__qbHandler({
            type: 'candidates', items: [], error: 'Vault is locked.',
            document_id: window.__quteBitwarden.documentId,
            form_id: %r
        });
    """ % fields['form_id'])
    count = _js(js_tester_webengine,
                "document.querySelectorAll("
                "'[data-qute-bitwarden-menu]').length",
                world=usertypes.JsWorld.main)
    state = _json(js_tester_webengine, """JSON.stringify({
        error: window.__quteBitwarden.menuError,
        form: window.__quteBitwarden.menuFormId,
        active: !!window.__quteBitwarden.activeField,
        activeForm: window.__quteBitwarden.activeFormId,
        host: !!window.__quteBitwarden.menuHost
    })""")
    assert count == 1, state
    assert state['error'] == 'Vault is locked.'
    assert state['form'] == fields['form_id']


def test_search_reply_does_not_open_closed_menu(qtbot, js_tester_webengine):
    _setup(js_tester_webengine, 'bitwarden_login.html')
    _js(js_tester_webengine, """
        window.__qbHandler({
            type: 'searchResults', items: [{id: '1', name: 'x'}],
            request_id: 1, document_id: window.__quteBitwarden.documentId,
            form_id: 'form-1'
        });
    """)
    count = _js(js_tester_webengine,
                "document.querySelectorAll("
                "'[data-qute-bitwarden-menu]').length",
                world=usertypes.JsWorld.main)
    assert count == 0
