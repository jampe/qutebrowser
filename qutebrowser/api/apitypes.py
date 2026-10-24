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
from qutebrowser.utils.usertypes import ClickTarget, JsWorld, InjectionPoint  # noqa: F401
from qutebrowser.extensions.loader import InitContext  # noqa: F401


class Frame:

    """A backend-neutral handle for one frame of a tab.

    Wraps a backend frame object; do not expose the raw QWebEngineFrame to
    components. Frames are ephemeral: check ``is_valid`` before use and never
    cache them across navigations.
    """

    def __init__(self, tab: 'ExtensionTab', raw: Any) -> None:
        self._tab = tab
        self._raw = raw
        self._origin_override: str | None = None

    def __repr__(self) -> str:
        return f'<Frame tab={self._tab!r} url={self.url.toString()!r}>'

    @property
    def tab(self) -> 'ExtensionTab':
        return self._tab

    @property
    def url(self) -> QUrl:
        return self._raw.url()

    @property
    def origin(self) -> str | None:
        if self._origin_override is not None:
            return self._origin_override
        return _url_origin(self.url)

    @property
    def is_main(self) -> bool:
        return bool(self._raw.isMainFrame())

    @property
    def is_valid(self) -> bool:
        return bool(self._raw.isValid())

    @property
    def _backend_frame(self) -> Any:
        """The raw backend frame, for internal use only."""
        return self._raw


def _url_origin(url: QUrl) -> str | None:
    """Get a normalized origin for a URL, or None for an opaque origin."""
    scheme = url.scheme()
    host = url.host()
    if scheme not in {'http', 'https', 'ws', 'wss', 'ftp'} or not host:
        return None
    default_ports = {'http': 80, 'https': 443, 'ws': 80, 'wss': 443, 'ftp': 21}
    port = url.port()
    if port != -1 and port != default_ports[scheme]:
        return f'{scheme}://{host}:{port}'
    return f'{scheme}://{host}'


def _is_same_site(first: QUrl, second: QUrl) -> bool:
    """Approximate same-site check for secret delivery.

    Without a PSL dependency this is deliberately conservative: exact host or
    a parent/child host relationship on the same scheme.
    """
    first_origin = _url_origin(first)
    second_origin = _url_origin(second)
    if first_origin is None or second_origin is None:
        return False
    if first.scheme() != second.scheme():
        return False
    first_host = first.host()
    second_host = second.host()
    return (
        first_host == second_host
        or first_host.endswith('.' + second_host)
        or second_host.endswith('.' + first_host)
    )


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

    @property
    def messaging_supported(self) -> bool:
        return bool(self._tab().messaging_supported)

    @property
    def site_isolation(self) -> bool:
        return bool(self._tab().site_isolation)

    @property
    def can_fill_cross_site(self) -> bool:
        return bool(self._tab().can_fill_cross_site)

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

    def frames(self) -> list[Frame]:
        """Get the frames of the current document.

        Returns only the main frame on Qt < 6.8 or on the WebKit backend.
        """
        return [Frame(self, raw) for raw in self._tab().frames()]

    def run_js_async(
            self,
            code: str,
            callback: Callable[[Any], None] | None = None, *,
            world: JsWorld | None = None,
            frame: Frame | None = None,
            name: str = '',
    ) -> None:
        raw_frame = None
        if frame is not None:
            if frame.tab is not self:
                raise WebTabError("Frame belongs to a different tab")
            if not frame.is_valid:
                raise WebTabError("Frame is no longer valid")
            raw_frame = frame._backend_frame
        self._tab().run_js_async(
            code, callback, world=world, frame=raw_frame, name=name)

    def send_message(
            self,
            payload: dict[str, Any], *,
            frame: Frame | None = None,
    ) -> None:
        """Send a JSON payload to one frame, guarded by its document token.

        ``frame=None`` targets the main frame. Cross-site frames are refused
        in v1; only the main frame and same-site subframes are allowed.
        """
        if not self.messaging_supported:
            raise UnsupportedOperationError(
                "Extension messaging is not available")

        raw_frame = None
        if frame is not None:
            if frame.tab is not self:
                raise WebTabError("Frame belongs to a different tab")
            if not frame.is_valid:
                raise WebTabError("Frame is no longer valid")
            if _url_origin(frame.url) is None:
                raise WebTabError("Refusing to fill an opaque-origin frame")
            if (not frame.is_main
                    and not _is_same_site(self.url(), frame.url)):
                raise WebTabError(
                    "Refusing to send secrets to a cross-site frame")
            raw_frame = frame._backend_frame

        self._tab().send_message(payload, frame=raw_frame)
