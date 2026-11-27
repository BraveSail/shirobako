"""Native article structure from the article HTML, not the CLI Markdown."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parsehub import ParseHub
from parsehub.parsers.parser.weixin import WXParser
from parsehub.provider_api.weixin import WX
from parsehub.types import DownloadError, ParseError, Platform
from parsehub.types.serialize import ResultRebuildUnavailable, result_from_cache_dict, result_to_cache_dict
from parsehub.utils import http

URL = "https://mp.weixin.qq.com/s/example-article"
PAGE = Path(__file__).parent / "fixtures" / "weixin_article_sections.html"


def test_weixin_section_title_is_a_heading_but_caption_and_partial_bold_are_not():
    article = WX._parse_data(PAGE.read_text())
    assert article.document is not None
    header, subtitle, photo, paragraph, caption, partial, plain = article.document.blocks
    assert (header.kind, header.level, header.runs[0].styles) == ("heading", 2, ("bold",))
    assert header.runs[0].text == "章节标题"
    assert subtitle.kind == "paragraph" and subtitle.runs[0].text == "副标题"
    assert (photo.kind, photo.index, photo.url) == ("media", 0, article.imgs[0])
    assert [(run.text, run.styles) for run in paragraph.runs] == [("问：", ("bold",)), ("正文 ||原样||", ())]
    assert [b.kind for b in (caption, partial, plain)] == ["paragraph"] * 3
    assert "章节标题" in article.markdown_content and "##" not in article.markdown_content


@pytest.fixture
def article_result(monkeypatch):
    get = AsyncMock(return_value=SimpleNamespace(text=PAGE.read_text()))
    with monkeypatch.context() as scope:
        scope.setattr(http.AsyncClient, "get", get)
        result = asyncio.run(ParseHub().parse(URL))
    get.assert_awaited_once()
    assert result.platform is Platform.WEIXIN
    return result


def test_cached_weixin_article_still_requires_downloading_its_inline_media(article_result):
    payload = json.loads(json.dumps(result_to_cache_dict(article_result)))
    warm = result_from_cache_dict(payload)
    assert type(warm) is type(article_result)
    assert warm.document == article_result.document
    assert article_result.requires_media_download and warm.requires_media_download


def test_only_weixin_legacy_result_is_invalidated(article_result):
    legacy = result_to_cache_dict(article_result) | {"content_version": 15, "impl": "RichTextParseResult"}
    with pytest.raises(ResultRebuildUnavailable):
        result_from_cache_dict(legacy)
    assert result_from_cache_dict(legacy | {"platform": "zhihu"}).platform is Platform.ZHIHU


def test_weixin_parser_requires_the_exact_article_host():
    assert WXParser.match(URL)
    assert not WXParser.match("https://mpXweixin.qq.com/s/example-article")


def test_weixin_article_page_uses_its_https_origin_without_redirecting(monkeypatch):
    get = AsyncMock(return_value=SimpleNamespace(text=PAGE.read_text()))
    monkeypatch.setattr(http.AsyncClient, "get", get)
    asyncio.run(WX.parse(URL.replace("https:", "http:", 1)))
    call = get.await_args
    assert call is not None and call.args == (URL,)
    assert call.kwargs["follow_redirects"] is False
    with pytest.raises(ParseError):
        asyncio.run(WX.parse("https://outside.invalid/s/example-article"))
    assert get.await_count == 1


def test_weixin_only_binds_cdn_images_and_download_uses_a_safe_suffix():
    source = (
        '<img data-src="https://outside.invalid/photo.png">'
        '<img data-src="//mmbiz.qpic.cn/a?wx_fmt=png">'
        '<img data-src="https://mmbiz.qlogo.cn/b?wx_fmt=odd/name">'
        '<img data-src="https://mmbiz.qpic.cn:8443/c">'
    )
    payload = {"item_show_type": 0, "content_noencode": source}
    article = WX._parse_data(f'<script>var cgiDataNew = {json.dumps(payload)};</script>')
    assert article.imgs == ['https://mmbiz.qpic.cn/a?wx_fmt=png', 'https://mmbiz.qlogo.cn/b?wx_fmt=odd/name']
    assert article.document is not None
    assert [b.index for b in article.document.blocks] == [0, 1]
    assert [ref.ext for ref in WXParser._refs(article.imgs)] == ['png', 'jpg']


def test_weixin_article_download_does_not_follow_cdn_redirects(article_result, monkeypatch, tmp_path):
    response = SimpleNamespace(status_code=302, headers={}, aclose=AsyncMock(), raise_for_status=lambda: None)
    methods = []

    async def request(_, method, url, **kwargs):
        assert url == article_result.media[0].url
        redirect_setting = kwargs.get('follow_redirects', kwargs.get('allow_redirects'))
        assert redirect_setting is False, f'{method} may follow a redirect'
        methods.append(method)
        return response

    monkeypatch.setattr(http.AsyncClient, 'request', request)
    monkeypatch.setattr('parsehub.utils.downloader._retry_delay', lambda *args, **kwargs: 0)
    with pytest.raises(DownloadError, match='302'):
        asyncio.run(article_result.download(tmp_path, connections=1))
    assert methods == ["HEAD", "GET"]
