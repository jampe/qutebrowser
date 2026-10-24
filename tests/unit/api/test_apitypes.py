# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the extension-facing tab adapter."""

import gc
import types
import weakref
from typing import Any, cast

import pytest

from qutebrowser.qt.core import QUrl

from qutebrowser.api import apitypes
from qutebrowser.browser import browsertab
from qutebrowser.utils import usertypes


class FakeTab:

    """Minimal stand-in for AbstractTab."""

    def __init__(self) -> None:
        self.tab_id = 42
        self.is_private = False
        self.data = types.SimpleNamespace(last_navigation=None)
        self.calls: list[tuple[Any, ...]] = []

    def url(self) -> QUrl:
        return QUrl('https://example.com/')

    def title(self) -> str:
        return 'Example'

    def add_dynamic_css(self, name: str, css: str) -> None:
        self.calls.append(('add_dynamic_css', name, css))

    def remove_dynamic_css(self, name: str) -> None:
        self.calls.append(('remove_dynamic_css', name))

    def add_web_script(self, name: str, js_code: str, **kwargs: Any) -> None:
        self.calls.append(('add_web_script', name, js_code, kwargs))

    def remove_web_script(self, name: str) -> None:
        self.calls.append(('remove_web_script', name))

    def run_js_async(self, code: str, callback: Any, **kwargs: Any) -> None:
        self.calls.append(('run_js_async', code, callback, kwargs))


@pytest.fixture
def fake_tab() -> FakeTab:
    return FakeTab()


@pytest.fixture
def adapter(fake_tab: FakeTab) -> apitypes.ExtensionTab:
    return apitypes.ExtensionTab(cast(Any, fake_tab))


def test_basic_properties(adapter, fake_tab):
    assert adapter.tab_id == 42
    assert not adapter.is_private
    assert adapter.url() == QUrl('https://example.com/')
    assert adapter.title() == 'Example'
    assert adapter.last_navigation_url is None


def test_last_navigation_url(adapter, fake_tab):
    fake_tab.data.last_navigation = types.SimpleNamespace(
        url=QUrl('https://example.com/login'))
    assert adapter.last_navigation_url == QUrl('https://example.com/login')


def test_css_delegation(adapter, fake_tab):
    adapter.add_dynamic_css('name', 'body {}')
    adapter.remove_dynamic_css('name')
    assert fake_tab.calls == [
        ('add_dynamic_css', 'name', 'body {}'),
        ('remove_dynamic_css', 'name'),
    ]


def test_web_script_defaults_are_forwarded(adapter, fake_tab):
    adapter.add_web_script('name', 'js')
    assert fake_tab.calls == [
        ('add_web_script', 'name', 'js', {
            'world': None,
            'injection_point': usertypes.InjectionPoint.creation,
            'subframes': False,
        }),
    ]


def test_run_js_async_delegation(adapter, fake_tab):
    adapter.run_js_async('1 + 1', name='test')
    assert fake_tab.calls == [
        ('run_js_async', '1 + 1', None, {'world': None, 'name': 'test'}),
    ]


def test_adapter_is_canonical_and_strongly_owned():
    fake_tab = FakeTab()
    fake_tab._api_adapter = None  # type: ignore[attr-defined]
    first = browsertab.AbstractTab.api_adapter(fake_tab)
    second = browsertab.AbstractTab.api_adapter(fake_tab)
    assert first is second
    assert first is fake_tab._api_adapter  # type: ignore[attr-defined]

    ref = weakref.ref(first)
    del first, second
    gc.collect()
    # The real tab owns the adapter, so it must survive local deletion.
    assert ref() is not None

    del fake_tab
    gc.collect()
    assert ref() is None


def test_adapter_raises_after_tab_is_gone():
    fake_tab = FakeTab()
    adapter = apitypes.ExtensionTab(cast(Any, fake_tab))
    del fake_tab
    gc.collect()
    with pytest.raises(browsertab.WebTabError, match='Tab is gone'):
        _ = adapter.tab_id
