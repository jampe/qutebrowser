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


class FakeFrame:

    """Minimal stand-in for a backend frame."""

    def __init__(self, url: str, *, is_main: bool = False,
                 valid: bool = True) -> None:
        self._url = QUrl(url)
        self._is_main = is_main
        self._valid = valid
        self.calls: list[tuple[Any, ...]] = []

    def url(self) -> QUrl:
        return self._url

    def isMainFrame(self) -> bool:
        return self._is_main

    def isValid(self) -> bool:
        return self._valid

    def runJavaScript(self, code: str, world: int,
                      callback: Any = None) -> None:
        self.calls.append((code, world, callback))


class FakeTab:

    """Minimal stand-in for AbstractTab."""

    def __init__(self) -> None:
        self.tab_id = 42
        self.is_private = False
        self.data = types.SimpleNamespace(last_navigation=None)
        self.frames_result: list[FakeFrame] = []
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

    def frames(self) -> list[FakeFrame]:
        return self.frames_result

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
        ('run_js_async', '1 + 1', None,
         {'world': None, 'frame': None, 'name': 'test'}),
    ]


def test_frames_are_wrapped(adapter, fake_tab):
    raw_main = FakeFrame('https://example.com/', is_main=True)
    raw_child = FakeFrame('https://other.example/', valid=False)
    fake_tab.frames_result = [raw_main, raw_child]

    frames = adapter.frames()
    assert [frame.tab for frame in frames] == [adapter, adapter]
    assert frames[0].url == QUrl('https://example.com/')
    assert frames[0].origin == 'https://example.com'
    assert frames[0].is_main
    assert frames[0].is_valid
    assert not frames[1].is_valid
    assert frames[1].origin == 'https://other.example'


@pytest.mark.parametrize('url, origin', [
    ('https://example.com/path', 'https://example.com'),
    ('https://example.com:443/path', 'https://example.com'),
    ('http://example.com:8080/path', 'http://example.com:8080'),
    ('about:blank', None),
    ('data:text/html,hello', None),
])
def test_frame_origin(adapter, url, origin):
    fake_tab = adapter._tab()
    fake_tab.frames_result = [FakeFrame(url)]
    assert adapter.frames()[0].origin == origin


def test_run_js_async_with_frame(adapter, fake_tab):
    raw = FakeFrame('https://example.com/')
    fake_tab.frames_result = [raw]
    frame = adapter.frames()[0]

    adapter.run_js_async('1 + 1', frame=frame, world=usertypes.JsWorld.main)
    assert fake_tab.calls == [
        ('run_js_async', '1 + 1', None,
         {'world': usertypes.JsWorld.main, 'frame': raw, 'name': ''}),
    ]


def test_run_js_async_rejects_invalid_frame(adapter, fake_tab):
    fake_tab.frames_result = [FakeFrame('https://example.com/', valid=False)]
    frame = adapter.frames()[0]
    with pytest.raises(browsertab.WebTabError, match='no longer valid'):
        adapter.run_js_async('1', frame=frame)


def test_run_js_async_rejects_foreign_frame(adapter, fake_tab):
    other_tab = FakeTab()
    other = apitypes.ExtensionTab(cast(Any, other_tab))
    raw = FakeFrame('https://example.com/')
    other_tab.frames_result = [raw]
    frame = other.frames()[0]
    with pytest.raises(browsertab.WebTabError, match='different tab'):
        adapter.run_js_async('1', frame=frame)


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
