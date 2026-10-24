# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A single tab."""

import weakref
from collections.abc import Callable
from typing import Any

from qutebrowser.qt.core import QUrl

# pylint: disable=unused-import
from qutebrowser.browser.browsertab import (  # noqa: F401
    UnsupportedOperationError,
    WebTabError,
    AbstractTab as Tab,
)
from qutebrowser.browser.inspector import (  # noqa: F401
    Position as InspectorPosition,
    Error as InspectorError,
)
from qutebrowser.browser.webelem import (  # noqa: F401
    Error as WebElemError,
    AbstractWebElement as WebElement,
)
from qutebrowser.utils import usertypes
from qutebrowser.utils.usertypes import ClickTarget, JsWorld  # noqa: F401
from qutebrowser.extensions.loader import InitContext  # noqa: F401


class ExtensionTab:

    """Narrow tab API exposed to components/extensions.

    This deliberately exposes only a stable subset of
    :class:`qutebrowser.browser.browsertab.AbstractTab`. It is strongly owned
    by the real tab (see ``AbstractTab.api_adapter()``) and only keeps a weak
    reference back, so components can hold it for the tab's lifetime without
    creating a reference cycle.
    """

    def __init__(self, tab: Tab) -> None:
        self._tab_ref: weakref.ReferenceType[Tab] = weakref.ref(tab)

    def _tab(self) -> Tab:
        tab = self._tab_ref()
        if tab is None:
            raise WebTabError("Tab is gone")
        return tab

    def __repr__(self) -> str:
        tab = self._tab_ref()
        tab_id = None if tab is None else tab.tab_id
        return f'<ExtensionTab tab_id={tab_id}>'

    @property
    def tab_id(self) -> int:
        return self._tab().tab_id

    @property
    def is_private(self) -> bool:
        return self._tab().is_private

    @property
    def last_navigation_url(self) -> QUrl | None:
        """The URL of the last main-frame navigation, if any."""
        navigation = self._tab().data.last_navigation
        return None if navigation is None else QUrl(navigation.url)

    def url(self) -> QUrl:
        return self._tab().url()

    def title(self) -> str:
        return self._tab().title()

    def add_dynamic_css(self, name: str, css: str) -> None:
        self._tab().add_dynamic_css(name, css)

    def remove_dynamic_css(self, name: str) -> None:
        self._tab().remove_dynamic_css(name)

    def add_web_script(
            self,
            name: str,
            js_code: str, *,
            world: JsWorld | None = None,
            injection_point: usertypes.InjectionPoint = (
                usertypes.InjectionPoint.creation),
            subframes: bool = False,
    ) -> None:
        self._tab().add_web_script(
            name,
            js_code,
            world=world,
            injection_point=injection_point,
            subframes=subframes,
        )

    def remove_web_script(self, name: str) -> None:
        self._tab().remove_web_script(name)

    def run_js_async(
            self,
            code: str,
            callback: Callable[[Any], None] | None = None, *,
            world: JsWorld | None = None,
            name: str = '',
    ) -> None:
        self._tab().run_js_async(
            code, callback, world=world, name=name)
