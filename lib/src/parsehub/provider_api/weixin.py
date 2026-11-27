import re
from dataclasses import dataclass
from enum import IntEnum
from html import unescape
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import json5
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from parsehub.types.document import Document, html_blocks, plain

from ..errors import ParseError
from ..utils import http
from ..utils.helpers import UA, get_author_name


class WXItemShowType(IntEnum):
    """页面数据里的 ``item_show_type``, 决定正文与媒体的取值位置."""

    ARTICLE = 0
    """公众号文章"""
    IMAGE = 8
    """图文"""
    TEXT = 10
    """纯文本"""


_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

#: 页面数据用 '0' * 1 这类写法做隐式类型转换, 不是合法的 JSON5 值
_IMPLICIT_INT_RE = re.compile(r"('[^'\n]*'|\"[^\"\n]*\")\s*\*\s*1(?![.\d])")
#: 对象以 "};" 结束后还跟着 try/catch 与 IIFE 收尾
_OBJECT_END_RE = re.compile(r"}\s*;.*$", re.DOTALL)

_PAYLOAD_NAME = "cgiDataNew"
_ARTICLE_IMAGE_HOSTS = frozenset({"mmbiz.qpic.cn", "mmbiz.qlogo.cn"})


def _load_payload(html: str) -> dict[str, Any]:
    """解析 ``window.cgiDataNew`` 为字典.

    数据在 script 标签里, 是 JS 对象字面量 (键无引号、单引号字符串、尾随逗号),
    用 json5 解析, 只需去掉两处 JS 语法.
    """
    scripts = [s.text for s in BeautifulSoup(html, "lxml").find_all("script") if _PAYLOAD_NAME in s.text]
    if not scripts:
        raise ParseError(f"未找到 {_PAYLOAD_NAME} 数据")

    text = scripts[0].split(_PAYLOAD_NAME, 1)[1]
    text = _IMPLICIT_INT_RE.sub(r"\1", text)
    text = text[text.index("{") :]
    return cast(dict[str, Any], json5.loads(_OBJECT_END_RE.sub("}", text, count=1)))


def _to_text(source: Any) -> str:
    """正文片段转纯文本, 兼容本身就是纯文本的情况."""
    if not isinstance(source, str) or not source:
        return ""
    return unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", unescape(source)))).strip()


def _collect_photos(data: dict[str, Any]) -> list[str]:
    """图片消息的图片地址, 取原始图 ``watermark_info.cdn_url``, 缺失时退回 ``cdn_url``."""
    entries = data.get("picture_page_info_list") or []
    return [
        url
        for e in entries
        if isinstance(e, dict)
        for url in [(e.get("watermark_info") or {}).get("cdn_url") or e.get("cdn_url")]
        if url
    ]


def _article_image_url(base_url: str, value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(urljoin(base_url, value))
        host = parsed.hostname
        if not host or host not in _ARTICLE_IMAGE_HOSTS or parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.scheme == "http" and parsed.port in (None, 80):
            return parsed._replace(scheme="https", netloc=host).geturl()
        if parsed.scheme == "https" and parsed.port in (None, 443):
            return parsed.geturl()
    except ValueError:
        pass
    return ""


def _mark_article_headings(soup: BeautifulSoup) -> None:
    """微信编辑器用居中的双段落表示章节标题和副标题，不输出 h2。"""
    for group in soup.find_all("section", style=True):
        styles = {
            key.strip().lower(): value.strip().lower()
            for part in str(group["style"]).split(";")
            for key, sep, value in [part.partition(":")]
            if sep
        }
        if styles.get("text-align") != "center":
            continue
        paragraphs = group.find_all(recursive=False)
        if len(paragraphs) != 2 or any(p.name != "p" or p.find("img") for p in paragraphs):
            continue
        title, subtitle = paragraphs
        bold = title.find(("strong", "b"))
        if (
            bold
            and (text := bold.get_text(strip=True))
            and title.get_text(strip=True) == text
            and subtitle.get_text(strip=True)
        ):
            title.name = "h2"


class WXConverter(MarkdownConverter):
    """把正文 HTML 转为 markdown, 图片地址取自 ``data-src``."""

    def convert_img(self, el: Any, text: Any, parent_tags: Any) -> str:
        alt = el.attrs.get("alt", None) or ""
        src = el.attrs.get("data-src", None) or el.attrs.get("src", None) or ""
        title = el.attrs.get("title", None) or ""
        title_part = ' "{}"'.format(title.replace('"', r"\"")) if title else ""
        options = cast(dict[str, Any], getattr(self, "options"))  # noqa: B009
        if "_inline" in parent_tags and el.parent.name not in options["keep_inline_images_in"]:
            return alt

        return f"![{alt}]({src}{title_part})"


@dataclass
class WX:
    """微信文章的解析结果.

    Attributes:
        item_show_type: 内容形态
        title: 标题
        markdown_content: markdown 正文, 仅图文形态有值
        imgs: 正文内的图片地址
        text_content: 纯文本正文
        photos: 图片消息的图片地址, 仅图片形态有值
        author_name: 公众号名
    """

    item_show_type: WXItemShowType
    title: str
    markdown_content: str
    imgs: list[str]
    text_content: str
    photos: list[str]
    author_name: str = ""
    document: Document | None = None

    @staticmethod
    async def parse(url: str, proxy: str | None = None) -> "WX":
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.netloc.lower() != "mp.weixin.qq.com":
            raise ParseError("不是微信公众平台文章地址")
        url = parsed._replace(scheme="https").geturl()
        async with http.AsyncClient(proxy=proxy) as client:
            response = await client.get(url, headers={"User-Agent": UA}, follow_redirects=False)
        return WX._parse_data(response.text, base_url=url)

    @classmethod
    def _parse_data(cls, html: str, *, base_url: str = "https://mp.weixin.qq.com/") -> "WX":
        data = _load_payload(html)
        try:
            item_show_type = WXItemShowType(int(data["item_show_type"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(f"不支持的内容类型: {data.get('item_show_type')!r}") from e

        author_name = get_author_name(data, "nick_name", "nickname", "author")

        match item_show_type:
            case WXItemShowType.ARTICLE:
                source = data.get("content_noencode") or ""
                soup = BeautifulSoup(source, "lxml")
                imgs: list[str] = []
                media: dict[str, int] = {}
                for img in soup.find_all("img"):
                    if not (src := _article_image_url(base_url, str(img.get("data-src") or "").strip())):
                        img.decompose()
                        continue
                    media.setdefault(src, len(imgs))
                    imgs.append(src)
                    img.attrs = {"src": src}
                _mark_article_headings(soup)
                return cls(
                    item_show_type=item_show_type,
                    title=data.get("title") or "",
                    markdown_content=WXConverter(heading_style="ATX").convert(source),
                    imgs=imgs,
                    text_content=_to_text(source),
                    photos=[],
                    author_name=author_name,
                    document=Document(html_blocks(soup, base_url=base_url, media=media)),
                )
            case WXItemShowType.TEXT:
                text = _to_text((data.get("text_page_info") or {}).get("content_noencode"))
                return cls(
                    item_show_type=item_show_type,
                    title="",
                    markdown_content="",
                    imgs=[],
                    text_content=text,
                    photos=[],
                    author_name=author_name,
                    document=plain(text),
                )
            case WXItemShowType.IMAGE:
                text = _to_text(data.get("content_noencode"))
                return cls(
                    item_show_type=item_show_type,
                    title=data.get("title") or "",
                    markdown_content="",
                    imgs=[],
                    text_content=text,
                    photos=_collect_photos(data),
                    author_name=author_name,
                    document=plain(text),
                )


__all__ = ["WX", "WXConverter", "WXItemShowType"]
