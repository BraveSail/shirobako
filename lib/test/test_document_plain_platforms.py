"""Literal platform adapters produce Documents through the public parser entry point."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from _fakes import FakeResponse, patch_async_get

from parsehub.parsers.parser import snapchat
from parsehub.parsers.parser.facebook import FacebookParse
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.parsers.parser.kuaishou import KuaiShouParser
from parsehub.parsers.parser.pipix import PipixParser
from parsehub.parsers.parser.pixiv import PixivParser
from parsehub.parsers.parser.snapchat import Snapchatarse
from parsehub.parsers.parser.tieba import TieBaParser
from parsehub.parsers.parser.tiktok import TikTokParser
from parsehub.parsers.parser.xhs import XHSParser
from parsehub.parsers.parser.zuiyou import ZuiYouParser
from parsehub.provider_api.facebook import parse_video_html
from parsehub.provider_api.instagram import InstagramAPI, InstagramPost
from parsehub.provider_api.kuaishou import KuaiShouAPI, KuaishouParser
from parsehub.provider_api.pipix import Pipix, PipixPost, PipixPostType
from parsehub.provider_api.pixiv import Pixiv, PixivIllust
from parsehub.provider_api.snapchat import SnapchatVideo, parse_spotlight
from parsehub.provider_api.tieba import TieBa, TieBaPhoto, TieBaPost, TieBaPostType, TieBaVideo
from parsehub.provider_api.tiktok import TikTokWebCrawler
from parsehub.provider_api.xhs import XHSAPI, XHSMedia, XHSMediaType, XHSPost, XHSPostType
from parsehub.provider_api.zuiyou import ZuiYou, ZuiYouPost
from parsehub.types import ImageParseResult, LivePhotoRef, MultimediaParseResult, VideoParseResult, VideoRef
from parsehub.types.document import Block, Document, TextRun
from parsehub.utils import http

FIXTURES = Path(__file__).parent / "fixtures"
LITERAL = "    **不是加粗**\n\n[文字](https://example.org) <b>字面</b> || {{media:0}}\\_"
VIDEO = "https://cdn.example/video.mp4"
IMAGE = "https://cdn.example/image.jpg"
SPOTLIGHT_ID = "W7_EDlXWTBiXAEEniNoMPwAAYYWtidGhudGZpAX1TKn0JAX1TKnXJAAAAAA"


def literal_document(text):
    return Document((Block("paragraph", runs=(TextRun(text),)),)) if text else Document()


def parse_with_provider(monkeypatch, target, method, upstream, parser, url):
    monkeypatch.setattr(target, method, AsyncMock(return_value=upstream))
    return asyncio.run(parser().parse(url))


def media_urls(result):
    media = result.media
    if isinstance(media, VideoRef):
        return [media.url]
    return [item.url for item in media or []]


@pytest.mark.parametrize(
    ("name", "ident", "path"),
    [
        ("watch", "647537299265662", "/watch/?v=647537299265662"),
        ("post", "6968553779868435", "/infopico/videos/6968553779868435"),
        ("reel", "1195289147628387", "/reel/1195289147628387"),
    ],
)
def test_facebook_caption_from_page(name, ident, path):
    html = (FIXTURES / f"facebook_{name}.html").read_text()
    source = parse_video_html(html, ident)
    with patch_async_get(FakeResponse(text=html)):
        result = asyncio.run(FacebookParse().parse(f"https://www.facebook.com{path}"))
    assert result.document == literal_document(source.content)
    assert result.title == source.title.strip()
    assert media_urls(result) == [source.url]


@pytest.mark.parametrize(
    ("typename", "result_type", "caption", "urls"),
    [
        ("GraphImage", ImageParseResult, LITERAL, [IMAGE]),
        ("GraphVideo", VideoParseResult, LITERAL, [VIDEO]),
        ("GraphSidecar", MultimediaParseResult, LITERAL, [IMAGE, VIDEO]),
        ("GraphImage", ImageParseResult, "", [IMAGE]),
    ],
)
def test_instagram_caption_is_literal(monkeypatch, typename, result_type, caption, urls):
    post = InstagramPost(
        {
            "__typename": typename,
            "is_video": typename == "GraphVideo",
            "title": "Title",
            "video_url": VIDEO,
            "display_url": IMAGE,
            "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]},
            "edge_sidecar_to_children": {
                "edges": [
                    {"node": {"display_url": IMAGE}},
                    {"node": {"is_video": True, "video_url": VIDEO, "display_url": IMAGE}},
                ]
            },
        }
    )
    result = parse_with_provider(
        monkeypatch, InstagramAPI, "get_post", post, InstagramParser, "https://www.instagram.com/p/example/"
    )
    assert result.document == literal_document(caption)
    assert result.title == "Title"
    assert isinstance(result, result_type)
    assert media_urls(result) == urls


@pytest.mark.parametrize("branch", ["video", "api-fallback", "album", "cover-only"])
def test_kuaishou_caption_stays_in_title(monkeypatch, branch):
    def get_video():
        if branch == "api-fallback":
            raise ValueError("HTML video unavailable")
        return SimpleNamespace(url=VIDEO, d=1, h=1, w=1)

    page = SimpleNamespace(
        page_type="VIDEO" if branch in {"video", "api-fallback"} else "ATLAS",
        get_title_content=lambda: LITERAL,
        get_cover_photo_url=lambda: IMAGE,
        get_author_name=lambda: "Author",
        get_real_video_url=get_video,
        get_image_list=lambda: [SimpleNamespace(url=IMAGE, h=1, w=1, e="jpg")] if branch == "album" else [],
    )
    api = AsyncMock(
        return_value=SimpleNamespace(
            title=LITERAL,
            author_name="Author",
            video_url=VIDEO,
            thumb_url=IMAGE,
            duration=1,
            height=1,
            width=1,
        )
    )
    monkeypatch.setattr(KuaiShouAPI, "get_video_info", api)
    result = parse_with_provider(
        monkeypatch, KuaishouParser, "create", page, KuaiShouParser, "https://www.kuaishou.com/fw/photo/123"
    )
    assert result.document == Document()
    assert result.title == LITERAL.strip()
    assert not result.content
    assert media_urls(result) == [VIDEO if branch in {"video", "api-fallback"} else IMAGE]
    assert api.await_count == (branch == "api-fallback")


@pytest.mark.parametrize("video", [False, True])
def test_pipix_caption_stays_in_title(monkeypatch, video):
    post = PipixPost(
        PipixPostType.VIDEO if video else PipixPostType.IMAGE,
        LITERAL,
        img_url=[IMAGE],
        video_url=VIDEO if video else None,
    )
    result = parse_with_provider(monkeypatch, Pipix, "parse", post, PipixParser, "https://h5.pipix.com/ppx/item/123")
    assert result.document == Document()
    assert result.title == LITERAL.strip()
    assert not result.content
    assert media_urls(result) == [VIDEO if video else IMAGE]


def test_pixiv_provider_extracted_description_is_literal(monkeypatch):
    post = PixivIllust.parse(
        {"title": "Artwork", "description": "**字面** &lt;b&gt;字面&lt;/b&gt;"},
        [
            {"urls": {"original": IMAGE}, "width": 2, "height": 3},
        ],
    )
    result = parse_with_provider(monkeypatch, Pixiv, "parse", post, PixivParser, "https://www.pixiv.net/artworks/123")
    assert result.document == literal_document("**字面** <b>字面</b>")
    assert result.title == "Artwork"
    assert media_urls(result) == [IMAGE]


def test_snapchat_description_from_page():
    html = (FIXTURES / "snapchat_spotlight.html").read_text()
    source = parse_spotlight(html, SPOTLIGHT_ID)
    with patch_async_get(FakeResponse(text=html)):
        result = asyncio.run(Snapchatarse().parse(f"https://www.snapchat.com/spotlight/{SPOTLIGHT_ID}"))
    assert result.document == literal_document(source.description)
    assert result.title == source.title.strip()
    assert media_urls(result) == [source.url]


def test_snapchat_description_is_literal(monkeypatch):
    post = SnapchatVideo(SPOTLIGHT_ID, VIDEO, title="Title", description=LITERAL)
    result = parse_with_provider(
        monkeypatch, snapchat, "fetch_video", post, Snapchatarse, f"https://www.snapchat.com/spotlight/{SPOTLIGHT_ID}"
    )
    assert result.document == literal_document(LITERAL)


@pytest.mark.parametrize("video", [False, True])
def test_tieba_body_is_literal(monkeypatch, video):
    post = TieBaPost(
        TieBaPostType.VIDEO if video else TieBaPostType.PHOTO,
        "Title",
        LITERAL,
        TieBaVideo(VIDEO) if video else [TieBaPhoto(IMAGE)],
    )
    monkeypatch.setattr(http.AsyncClient, "head", AsyncMock(return_value=FakeResponse()))
    result = parse_with_provider(monkeypatch, TieBa, "parse", post, TieBaParser, "https://tieba.baidu.com/p/123")
    assert result.document == literal_document(LITERAL)
    assert result.title == "Title"
    assert media_urls(result) == [VIDEO if video else IMAGE]


@pytest.mark.parametrize("video", [False, True])
def test_tiktok_caption_stays_in_title(monkeypatch, video):
    payload: dict[str, Any] = {"desc": LITERAL}
    if video:
        payload["video"] = {"playAddr": VIDEO}
    else:
        payload["imagePost"] = {"images": [{"imageURL": {"urlList": [IMAGE]}}]}
    path = "video" if video else "photo"
    result = parse_with_provider(
        monkeypatch, TikTokWebCrawler, "parse", payload, TikTokParser, f"https://www.tiktok.com/@author/{path}/123"
    )
    assert result.document == Document()
    assert result.title == LITERAL.strip()
    assert not result.content
    assert media_urls(result) == [VIDEO if video else IMAGE]


@pytest.mark.parametrize("video", [False, True])
def test_xhs_body_keeps_topic_cleanup(monkeypatch, video):
    media = (
        [XHSMedia(XHSMediaType.VIDEO, VIDEO)]
        if video
        else [
            XHSMedia(XHSMediaType.IMAGE, IMAGE),
            XHSMedia(XHSMediaType.LIVE_PHOTO, VIDEO, thumb_url=IMAGE),
        ]
    )
    post = XHSPost(XHSPostType.VIDEO if video else XHSPostType.IMAGE, "Title", LITERAL + " #话题名[话题]#", media)
    monkeypatch.setattr(
        http.AsyncClient,
        "head",
        AsyncMock(
            return_value=FakeResponse(
                headers={"content-type": "image/jpeg"},
            )
        ),
    )
    result = parse_with_provider(
        monkeypatch, XHSAPI, "extract", post, XHSParser, "https://www.xiaohongshu.com/explore/123"
    )
    assert result.document == literal_document(LITERAL.strip() + "#话题名")
    assert result.title == "Title"
    if video:
        assert media_urls(result) == [VIDEO]
    else:
        assert result.media[0].url == IMAGE
        assert isinstance(result.media[1], LivePhotoRef)
        assert result.media[1].video_url == VIDEO


@pytest.mark.parametrize("with_media", [False, True])
def test_zuiyou_body_is_literal(monkeypatch, with_media):
    payload: dict[str, Any] = {"content": LITERAL}
    if with_media:
        images = {"original": {"urls": [IMAGE]}}
        payload["imgs"] = [{"id": 1, "urls": images}, {"id": 2, "urls": images, "video": True}]
        payload["videos"] = {"2": {"url": VIDEO}}
    post = ZuiYouPost.parse({"data": {"post": payload}})
    result = parse_with_provider(
        monkeypatch, ZuiYou, "parse", post, ZuiYouParser, "https://share.xiaochuankeji.cn/hybrid/share/post?pid=123"
    )
    assert result.document == literal_document(LITERAL)
    assert not result.title
    assert media_urls(result) == ([IMAGE, VIDEO] if with_media else [])
