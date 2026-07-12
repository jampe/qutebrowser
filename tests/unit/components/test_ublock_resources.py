# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import json
import importlib
import pathlib

import pytest

from qutebrowser.qt.core import QUrl
from qutebrowser.components import braveadblock

# I think the following needs to be written in this format for importlib.reload to have
# an effect.
# pylint: disable-next=consider-using-from-import
import qutebrowser.components.ublock_resources as ublock_resources  # noqa: I250
from helpers import testutils

pytestmark = pytest.mark.usefixtures("qapp")

REDIRECT_RESOURCES = [
    ("1x1.gif", ["1x1-transparent.gif"]),
    ("2x2.png", ["2x2-transparent.png"]),
    ("3x2.png", ["3x2-transparent.png"]),
    ("32x32.png", ["32x32-transparent.png"]),
    ("adthrive_abd.js", []),
    ("amazon_ads.js", ["amazon-adsystem.com/aax2/amzn_ads.js"]),
    ("amazon_apstag.js", []),
    ("ampproject_v0.js", ["ampproject.org/v0.js"]),
    ("chartbeat.js", ["static.chartbeat.com/chartbeat.js"]),
    ("doubleclick_instream_ad_status.js", ["doubleclick.net/instream/ad_status.js"]),
    ("empty", []),
    ("fingerprint2.js", []),
    ("fingerprint3.js", []),
    (
        "google-analytics_analytics.js",
        [
            "google-analytics.com/analytics.js",
            "googletagmanager_gtm.js",
            "googletagmanager.com/gtm.js",
        ],
    ),
    ("google-analytics_cx_api.js", ["google-analytics.com/cx/api.js"]),
    ("google-analytics_ga.js", ["google-analytics.com/ga.js"]),
    ("google-analytics_inpage_linkid.js", ["google-analytics.com/inpage_linkid.js"]),
    ("google-ima.js", ["google-ima3"]),
    (
        "googlesyndication_adsbygoogle.js",
        ["googlesyndication.com/adsbygoogle.js", "googlesyndication-adsbygoogle"],
    ),
    (
        "googletagservices_gpt.js",
        ["googletagservices.com/gpt.js", "googletagservices-gpt"],
    ),
    ("hd-main.js", []),
    ("nitropay_ads.js", []),
    ("nobab2.js", []),
    ("noeval.js", []),
    ("noeval-silent.js", ["silent-noeval.js"]),
    ("nofab.js", ["fuckadblock.js-3.2.0"]),
    ("noop-0.1s.mp3", ["noopmp3-0.1s", "abp-resource:blank-mp3"]),
    ("noop-0.5s.mp3", []),
    ("noop-1s.mp4", ["noopmp4-1s", "abp-resource:blank-mp4"]),
    ("noop.css", []),
    ("noop.html", ["noopframe"]),
    ("noop.js", ["noopjs", "abp-resource:blank-js"]),
    ("noop.json", ["noopjson"]),
    ("noop.txt", ["nooptext"]),
    ("noop-vast2.xml", ["noopvast-2.0"]),
    ("noop-vast3.xml", ["noopvast-3.0"]),
    ("noop-vast4.xml", ["noopvast-4.0"]),
    ("noop-vmap1.xml", ["noopvmap-1.0"]),
    ("outbrain-widget.js", ["widgets.outbrain.com/outbrain.js"]),
    ("popads.js", ["popads.net.js", "prevent-popads-net.js"]),
    ("popads-dummy.js", []),
    ("prebid-ads.js", []),
    ("scorecardresearch_beacon.js", ["scorecardresearch.com/beacon.js"]),
    ("sensors-analytics.js", []),
]

SCRIPTLET_RESOURCES = [
    ("safe-self.fn", [], "javascript"),
    ("get-random-token.fn", [], "javascript"),
    ("get-exception-token.fn", [], "javascript"),
    ("trap-property-access.fn", [], "javascript"),
    ("collate-fetch-arguments.fn", [], "javascript"),
    ("parse-properties-to-match.fn", [], "javascript"),
    ("match-object-properties.fn", [], "javascript"),
    ("generate-content.fn", [], "javascript"),
    ("on-idle.fn", [], "javascript"),
    ("off-idle.fn", [], "javascript"),
    ("sleep.fn", [], "javascript"),
    ("lookup-elements.fn", [], "javascript"),
    ("run-at.fn", [], "javascript"),
    ("run-at-html-element.fn", [], "javascript"),
    ("proxy-apply.fn", [], "javascript"),
    ("validate-constant.fn", [], "javascript"),
    ("set-constant.fn", [], "javascript"),
    ("set-constant.js", ["set.js"], "javascript"),
    ("trusted-set-constant.js", ["trusted-set.js"], "javascript"),
    ("get-safe-cookie-values.fn", [], "javascript"),
    ("get-all-cookies.fn", [], "javascript"),
    ("get-cookie.fn", [], "javascript"),
    ("set-cookie.fn", [], "javascript"),
    ("set-cookie.js", [], "javascript"),
    ("set-cookie-reload.js", [], "javascript"),
    ("trusted-set-cookie.js", [], "javascript"),
    ("trusted-set-cookie-reload.js", [], "javascript"),
    ("remove-cookie.js", ["cookie-remover.js"], "javascript"),
    ("get-all-local-storage.fn", [], "javascript"),
    ("set-local-storage-item.fn", [], "javascript"),
    ("remove-cache-storage-item.fn", [], "javascript"),
    ("set-local-storage-item.js", [], "javascript"),
    ("set-session-storage-item.js", [], "javascript"),
    ("trusted-set-local-storage-item.js", [], "javascript"),
    ("trusted-set-session-storage-item.js", [], "javascript"),
    ("matches-stack-trace.fn", [], "javascript"),
    ("abort-on-stack-trace.js", ["aost.js"], "javascript"),
    ("arglist-parser.fn", [], "javascript"),
    ("jsonpath.fn", [], "javascript"),
    ("parse-replace.fn", [], "javascript"),
    ("set-attr.fn", [], "javascript"),
    ("set-attr.js", [], "javascript"),
    ("trusted-set-attr.js", [], "javascript"),
    ("remove-attr.js", ["ra.js"], "javascript"),
    ("trusted-create-html.js", [], "javascript"),
    ("urlskip.fn", [], "javascript"),
    ("href-sanitizer.js", ["urlskip.js"], "javascript"),
    ("edit-outbound-object.fn", [], "javascript"),
    ("edit-outbound-object.js", [], "javascript"),
    ("trusted-edit-outbound-object.js", [], "javascript"),
    ("json-edit.js", [], "javascript"),
    ("trusted-json-edit.js", [], "javascript"),
    ("edit-inbound-object.fn", [], "javascript"),
    ("edit-inbound-object.js", [], "javascript"),
    ("trusted-edit-inbound-object.js", [], "javascript"),
    ("edit-this-object.fn", [], "javascript"),
    ("edit-this-object.js", [], "javascript"),
    ("trusted-edit-this-object.js", [], "javascript"),
    ("edit-object-on-getter.fn", [], "javascript"),
    ("edit-object-on-getter.js", [], "javascript"),
    ("trusted-edit-object-on-getter.js", [], "javascript"),
    ("edit-object-on-setter.fn", [], "javascript"),
    ("edit-object-on-setter.js", [], "javascript"),
    ("trusted-edit-object-on-setter.js", [], "javascript"),
    ("edit-element-object.fn", [], "javascript"),
    ("edit-element-object.js", [], "javascript"),
    ("trusted-edit-element-object.js", [], "javascript"),
    ("json-edit-xhr-response.fn", [], "javascript"),
    ("json-edit-xhr-response.js", [], "javascript"),
    ("trusted-json-edit-xhr-response.js", [], "javascript"),
    ("json-edit-xhr-request.fn", [], "javascript"),
    ("json-edit-xhr-request.js", [], "javascript"),
    ("trusted-json-edit-xhr-request.js", [], "javascript"),
    ("json-edit-fetch-response.fn", [], "javascript"),
    ("json-edit-fetch-response.js", [], "javascript"),
    ("trusted-json-edit-fetch-response.js", [], "javascript"),
    ("json-edit-fetch-request.fn", [], "javascript"),
    ("json-edit-fetch-request.js", [], "javascript"),
    ("trusted-json-edit-fetch-request.js", [], "javascript"),
    ("jsonl-edit.fn", [], "javascript"),
    ("jsonl-edit-xhr-response.fn", [], "javascript"),
    ("jsonl-edit-xhr-response.js", [], "javascript"),
    ("trusted-jsonl-edit-xhr-response.js", [], "javascript"),
    ("jsonl-edit-fetch-response.fn", [], "javascript"),
    ("jsonl-edit-fetch-response.js", [], "javascript"),
    ("trusted-jsonl-edit-fetch-response.js", [], "javascript"),
    ("json-prune.js", [], "javascript"),
    ("json-prune-fetch-response.js", [], "javascript"),
    ("json-prune-xhr-response.js", [], "javascript"),
    ("evaldata-prune.js", [], "javascript"),
    ("noeval-if.js", ["prevent-eval-if.js"], "javascript"),
    ("object-find-owner.fn", [], "javascript"),
    ("object-prune.fn", [], "javascript"),
    ("trusted-prune-inbound-object.js", [], "javascript"),
    ("trusted-prune-outbound-object.js", [], "javascript"),
    ("prevent-addEventListener.js", ["addEventListener-defuser.js", "aeld.js"], "javascript"),
    ("prevent-bab.js", ["bab-defuser.js", "nobab.js"], "javascript"),
    ("prevent-clipboard-write.js", [], "javascript"),
    ("prevent-dialog.js", [], "javascript"),
    ("prevent-fetch.fn", [], "javascript"),
    ("prevent-fetch.js", ["no-fetch-if.js"], "javascript"),
    ("trusted-prevent-fetch.js", [], "javascript"),
    ("freeze-element-property.js", [], "javascript"),
    ("prevent-innerHTML.js", [], "javascript"),
    ("prevent-navigation.js", [], "javascript"),
    ("range-parser.fn", [], "javascript"),
    ("prevent-setTimeout.js", ["no-setTimeout-if.js", "nostif.js", "setTimeout-defuser.js"], "javascript"),
    ("prevent-setInterval.js", ["no-setInterval-if.js", "nosiif.js", "setInterval-defuser.js"], "javascript"),
    ("prevent-requestAnimationFrame.js", ["no-requestAnimationFrame-if.js", "norafif.js"], "javascript"),
    ("prevent-xhr.fn", [], "javascript"),
    ("prevent-xhr.js", ["no-xhr-if.js"], "javascript"),
    ("trusted-prevent-xhr.js", [], "javascript"),
    ("trusted-replace-argument.js", [], "javascript"),
    ("spoof-css.js", [], "javascript"),
    ("should-debug.fn", [], "javascript"),
    ("abort-current-script.fn", [], "javascript"),
    ("replace-node-text.fn", [], "javascript"),
    ("replace-fetch-response.fn", [], "javascript"),
    ("abort-current-script.js", ["acs.js", "abort-current-inline-script.js", "acis.js"], "javascript"),
    ("abort-on-property-read.js", ["aopr.js"], "javascript"),
    ("abort-on-property-write.js", ["aopw.js"], "javascript"),
    ("adjust-setInterval.js", ["nano-setInterval-booster.js", "nano-sib.js"], "javascript"),
    ("adjust-setTimeout.js", ["nano-setTimeout-booster.js", "nano-stb.js"], "javascript"),
    ("prevent-refresh.js", ["refresh-defuser.js"], "javascript"),
    ("remove-class.js", ["rc.js"], "javascript"),
    ("webrtc-if.js", [], "javascript"),
    ("prevent-window-open.js", ["nowoif.js", "no-window-open-if.js", "window.open-defuser.js"], "javascript"),
    ("close-window.js", ["window-close-if.js"], "javascript"),
    ("window.name-defuser.js", [], "javascript"),
    ("overlay-buster.js", [], "javascript"),
    ("alert-buster.js", [], "javascript"),
    ("nowebrtc.js", [], "javascript"),
    ("disable-newtab-links.js", [], "javascript"),
    ("xml-prune.js", [], "javascript"),
    ("m3u-prune.js", [], "javascript"),
    ("call-nothrow.js", [], "javascript"),
    ("remove-node-text.js", ["rmnt.js"], "javascript"),
    ("prevent-canvas.js", [], "javascript"),
    ("multiup.js", [], "javascript"),
    ("break-on-call.js", [], "javascript"),
    ("trusted-replace-node-text.js", ["trusted-rpnt.js", "replace-node-text.js", "rpnt.js"], "javascript"),
    ("trusted-replace-fetch-response.js", ["trusted-rpfr.js"], "javascript"),
    ("trusted-replace-xhr-response.js", [], "javascript"),
    ("trusted-click-element.js", [], "javascript"),
    ("trusted-replace-outbound-text.js", [], "javascript"),
    ("trusted-suppress-native-method.js", [], "javascript"),
    ("trusted-prevent-dom-bypass.js", [], "javascript"),
    ("trusted-override-element-method.js", [], "javascript"),
]


def path_to_url(path):
    return QUrl(path.as_uri())


@pytest.fixture
def ad_blocker(data_tmpdir):
    pytest.importorskip("adblock")
    import adblock

    if adblock.__version__ < "0.6.0":
        pytest.skip()
    return braveadblock.BraveAdBlocker(data_dir=pathlib.Path(str(data_tmpdir)))


@pytest.fixture
def engine_info(ad_blocker):
    return ublock_resources.EngineInfo(
        ad_blocker._engine, ad_blocker._cache_path, ad_blocker._resources_cache_path
    )


@pytest.fixture
def local_resources(tmp_path):
    class LocalResource:
        def __init__(self, name):
            self.path = tmp_path / name

        @property
        def url(self):
            return path_to_url(self.path)

        def write(self, data: bytes):
            with open(self.path, "wb") as f:
                f.write(data)

    class LocalResourceFactory:
        def rsrc(self, name):
            return LocalResource(name)

        def url_template(self):
            return f"{tmp_path.as_uri()}/{{}}"

    return LocalResourceFactory()


@pytest.fixture
def redirect_resources(local_resources):

    ext_map = {
        ".gif": "image/gif",
        ".html": "text/html",
        ".js": "application/javascript",
        ".json": "application/json",
        ".mp3": "audio/mp3",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".txt": "text/plain",
        ".css": "text/css",
        ".xml": "application/octet-stream",
        "": "application/octet-stream",
    }

    resources = []
    for name, aliases in REDIRECT_RESOURCES:
        r = local_resources.rsrc(name)
        with testutils.ublock_resource(name) as f:
            data = f.read()
            r.write(data)

        content = data
        if ext_map[r.path.suffix] in (
            "application/javascript",
            "text/html",
            "text/plain",
            "text/css",
            "application/json",
        ):
            content = data.decode("utf-8").replace("\r", "").encode()

        resources.append(
            ublock_resources.Resource(
                name,
                aliases,
                ext_map[r.path.suffix],
                base64.b64encode(content).decode("utf-8"),
            )
        )

    return resources


@pytest.fixture
def scriptlet_resources(local_resources):
    ext_map = {
        "template": "template",
        "javascript": "application/javascript",
    }
    resources = []
    for name, aliases, content_type in SCRIPTLET_RESOURCES:
        r = local_resources.rsrc(name)
        with testutils.ublock_scriptlet_resource(name) as f:
            data = f.read()
            r.write(data)

        resources.append(
            ublock_resources.Resource(
                name,
                aliases,
                ext_map[content_type],
                base64.b64encode(data).decode("utf-8"),
            )
        )

    return resources


@pytest.fixture
def resource_aliases(local_resources):
    return {local_resources.rsrc(k).url.toString(): v for k, v in REDIRECT_RESOURCES}


@pytest.fixture
def redirect_resources_js(local_resources):
    r = local_resources.rsrc("redirect-resources.js")
    with testutils.ublock_redirect_resources_js() as f:
        r.write(f.read())
    return r


@pytest.fixture
def scriptlets_js(local_resources):
    r = local_resources.rsrc("scriptlets.js")
    with testutils.ublock_scriptlets_js() as f:
        r.write(f.read())
    return r


def test_redirect_engine_parsing(
    tmp_path,
    config_stub,
    engine_info,
    redirect_resources,
    scriptlets_js,
    redirect_resources_js,
    resource_aliases,
    local_resources,
):
    config_stub.val.content.blocking.enabled = True
    config_stub.val.content.blocking.method = "both"

    ublock_resources._WEB_ACCESSIBLE_RESOURCES_URL_TEMPLATE = (
        local_resources.url_template()
    )
    ublock_resources._SCRIPTLETS_URL = scriptlets_js.url

    def dummy_resource_dl(_, aliases_map, *args):
        assert aliases_map == resource_aliases

    ublock_resources._on_resource_download = dummy_resource_dl
    ublock_resources._on_all_resources_downloaded = lambda *_: None

    dl = ublock_resources._on_redirect_engine_download(
        QUrl(), testutils.ublock_redirect_resources_js(), engine_info
    )
    assert dl is not None
    assert dl._urls == [
        local_resources.rsrc(resource.name).url for resource in redirect_resources
    ] + [scriptlets_js.url]

    # This is important because we changed the ublock_resources module's values, and we
    # don't want it messed up for other tests
    importlib.reload(ublock_resources)


def test_parse_resource(
    config_stub,
    engine_info,
    redirect_resources,
    scriptlets_js,
    redirect_resources_js,
    resource_aliases,
    local_resources,
):
    config_stub.val.content.blocking.enabled = True
    config_stub.val.content.blocking.method = "both"

    resources = []
    for resource in redirect_resources:
        r = local_resources.rsrc(resource.name)
        with open(r.path, "rb") as f:
            ublock_resources._on_resource_download(
                resources,
                resource_aliases,
                r.url,
                f,
            )
    assert resources == redirect_resources


def test_parse_scriptlets(
    scriptlets_js,
    scriptlet_resources,
):
    ublock_resources._SCRIPTLETS_URL = scriptlets_js.url
    resources = []
    with open(scriptlets_js.path, "rb") as f:
        ublock_resources._on_resource_download(resources, {}, scriptlets_js.url, f)

    for r1, r2 in zip(resources, scriptlet_resources):
        if r1 != r2 and r1.content != r2.content:
            print(f"Failure to match {r1.name} == {r2.name}")
            r1_content = base64.b64decode(r1.content.encode()).decode("utf-8")
            r2_content = base64.b64decode(r2.content.encode()).decode("utf-8")
            print(f"r1.content.decode:\n'{r1_content}'")
            print(f"r2.content.decode:\n'{r2_content}'")

        assert r1 == r2

    # This is important because we changed the ublock_resources module's values, and we
    # don't want it messed up for other tests
    importlib.reload(ublock_resources)


def test_serialization(tmp_path, redirect_resources, scriptlet_resources):
    path = tmp_path / "adblock-resources-cache.dat"
    ublock_resources._serialize_resources_to_file(
        redirect_resources + scriptlet_resources, path
    )

    with open(path, "rb") as f1, testutils.ublock_resources_cache() as f2:
        f1_data = sorted(json.loads(f1.read().decode()), key=lambda r: r["name"])
        f2_data = sorted(json.loads(f2.read().decode()), key=lambda r: r["name"])
        assert {r["name"] for r in f1_data} == {r["name"] for r in f2_data}
        for r1, r2 in zip(f1_data, f2_data):
            assert r1 == r2

    resources = ublock_resources.deserialize_resources_from_file(path)
    assert resources == redirect_resources + scriptlet_resources


def test_resources_update(
    config_stub,
    ad_blocker,
    redirect_resources_js,
    scriptlets_js,
    redirect_resources,
    local_resources,
):
    config_stub.val.content.blocking.enabled = True
    config_stub.val.content.blocking.method = "both"

    ublock_resources._WEB_ACCESSIBLE_RESOURCES_URL_TEMPLATE = (
        local_resources.url_template()
    )
    ublock_resources._SCRIPTLETS_URL = scriptlets_js.url
    ublock_resources._REDIRECT_RESOURCES_URL = redirect_resources_js.url

    ad_blocker.resources_update()
    with open(
        ad_blocker._resources_cache_path, "rb"
    ) as f1, testutils.ublock_resources_cache() as f2:
        f1_data = sorted(json.loads(f1.read().decode()), key=lambda r: r["name"])
        f2_data = sorted(json.loads(f2.read().decode()), key=lambda r: r["name"])
        assert {r["name"] for r in f1_data} == {r["name"] for r in f2_data}
        for r1, r2 in zip(f1_data, f2_data):
            assert r1 == r2

    # This is important because we changed the ublock_resources module's values, and we
    # don't want it messed up for other tests
    importlib.reload(ublock_resources)
