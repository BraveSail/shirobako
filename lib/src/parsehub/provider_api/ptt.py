"""PTT（批踢踢實業坊）文章抓取与解析。

页面结构（2026-10 实测 `https://www.ptt.cc/bbs/C_Chat/M.1792932204.A.E69.html`）::

    <div id="main-content" class="bbs-screen bbs-content">
      <div class="article-metaline"><span class="article-meta-tag">作者</span>
        <span class="article-meta-value">anpinjou (大炎上…)</span></div>
      …看板 / 標題 / 時間…
      【正文纯文本】+ <a href="…">链接</a>
      <div class="push"><span class="hl push-tag">推 </span>
        <span class="f3 hl push-userid">gsock</span>
        <span class="f3 push-content">: 6</span>
        <span class="push-ipdatetime">10/25 20:43</span></div>
      …更多 push…
      <span class="f2">※ 發信站: …</span>
    </div>

要点：
- **正文是纯文本**（换行是 ``\\n``，不是 ``<br>``），只有链接是 ``<a>``；
- **图片是文本 URL**（``https://i.imgur.com/xxx.jpeg``），不是 ``<img>``；
- 推文（push）每条一个 ``div.push``，三类：推 / 噓 / →（箭头）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger

from ..utils import http

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

#: 正文里被当成图片的 URL（PTT 的图都是文字链接）
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|gif|webp|bmp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

#: 发信站 / 文章网址那两行签名（正文不要）
_SIGNATURE_RE = re.compile(r"^※\s*(發信站|文章網址|編輯|轉錄)", re.M)

#: 推文内文前缀（``: 内容``）
_PUSH_PREFIX = ": "


@dataclass
class PttPush:
    """一条推文。"""

    tag: str  # 推 / 噓 / →
    user: str
    content: str
    time: str = ""


@dataclass
class PttArticle:
    title: str = ""
    board: str = ""
    #: 显示名（PTT 作者格式 ``id (昵称)`` → 昵称，没有就是 id）
    author_name: str = ""
    #: 登录名（``id``）—— 用来拼个人看板链接
    author_handle: str = ""
    published_at: datetime | None = None
    body: str = ""
    images: list[str] = field(default_factory=list)
    pushes: list[PttPush] = field(default_factory=list)


#: PTT 时间是台北时间（UTC+8）
_TPE = timezone(timedelta(hours=8))


def _parse_time(value: str) -> datetime | None:
    """``Sun Oct 25 20:43:22 2026`` → 带时区的 datetime（PTT 全站台北时间）。"""
    text = (value or "").strip()
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b %d %H:%M %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_TPE)
        except ValueError:
            continue
    logger.debug(f"PTT 时间解析失败: {text!r}")
    return None


def _author_parts(value: str) -> tuple[str, str]:
    """``anpinjou (大炎上、確定ですわ。)`` → (显示名, 用户名)。

    PTT 的作者是 ``id (昵称)``；没有昵称时两者相同。
    """
    text = (value or "").strip()
    match = re.match(r"^(\S+)\s*\((.*)\)\s*$", text)
    if match:
        return (match.group(2).strip() or match.group(1)), match.group(1)
    return text, text


class PttAPI:
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy

    async def fetch(self, url: str) -> PttArticle:
        async with http.AsyncClient(proxy=self.proxy) as client:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return self.parse_html(response.text)

    @classmethod
    def parse_html(cls, html: str) -> PttArticle:
        soup = BeautifulSoup(html, "lxml")
        main = soup.find("div", id="main-content")
        if main is None:
            raise ValueError("PTT 页面里没有 #main-content")

        article = PttArticle()
        # ── metaline: 作者 / 看板 / 標題 / 時間 ──
        for block in main.find_all("div", class_="article-metaline"):
            tag = block.find("span", class_="article-meta-tag")
            value = block.find("span", class_="article-meta-value")
            if not tag or not value:
                continue
            key = tag.get_text(strip=True)
            text = value.get_text(strip=True)
            if key == "作者":
                article.author_name, article.author_handle = _author_parts(text)
            elif key == "看板":
                article.board = text
            elif key == "標題":
                article.title = text
            elif key == "時間":
                article.published_at = _parse_time(text)
        if not article.board:
            right = main.find("div", class_="article-metaline-right")
            if right:
                value = right.find("span", class_="article-meta-value")
                if value:
                    article.board = value.get_text(strip=True)

        # ── 推文 ──
        for push in main.find_all("div", class_="push"):
            tag_el = push.find("span", class_="push-tag")
            user_el = push.find("span", class_="push-userid")
            content_el = push.find("span", class_="push-content")
            time_el = push.find("span", class_="push-ipdatetime")
            article.pushes.append(
                PttPush(
                    tag=(tag_el.get_text(strip=True) if tag_el else ""),
                    user=(user_el.get_text(strip=True) if user_el else ""),
                    content=(content_el.get_text(strip=True) if content_el else "").removeprefix(_PUSH_PREFIX),
                    time=(time_el.get_text(strip=True) if time_el else ""),
                )
            )

        # ── 正文：main 里除 metaline / push / 签名之外的文本 ──
        article.body, article.images = cls._extract_body(main)
        return article

    @staticmethod
    def _extract_body(main: Any) -> tuple[str, list[str]]:
        """把正文拆出来：删掉 metaline / push / 签名，保留 ``<a>`` 的链接文字。

        返回 ``(正文, 图片列表)``。图片处理**必须在这里做**（而不是事后对整段文本
        再跑一次正则）：``<a href="…jpeg">…jpeg</a>`` 先被转成 ``[url](url)`` 再被
        替换，就会得到 ``[{{media:0}}]({{media:0}})`` —— 渲染出来是一个空链接
        ``[]()``（用户实测报过）。图片链接直接换成标记，不生成链接语法。
        """
        work = BeautifulSoup(str(main), "lxml")
        node = work.find("div", id="main-content") or work
        for block in node.find_all("div", class_=["article-metaline", "article-metaline-right", "push"]):
            block.decompose()
        # 签名行（※ 發信站 …）在 span.f2 里，整块去掉
        for span in node.find_all("span", class_="f2"):
            if _SIGNATURE_RE.search(span.get_text(" ", strip=True)):
                span.decompose()

        images: list[str] = []

        def _inline(text: str) -> str:
            """纯文本原样保留 —— 图片 URL 就是普通的链接文本。"""
            return text

        lines: list[str] = []
        for element in node.children:
            name = getattr(element, "name", None)
            if name == "a":
                href = element.get("href") or ""
                label = element.get_text(" ", strip=False).strip()
                if href and _IMAGE_URL_RE.fullmatch(href):
                    # PTT 的图片是**纯文本链接**，用户定：直接放链接、不抽成 media
                    # （抽了反而要额外请求下载，PTT 也没有图注/尺寸信息）。
                    lines.append(href)
                elif href:
                    lines.append(f"[{_inline(label) or href}]({href})")
                elif label:
                    lines.append(_inline(label))
                continue
            text = element.get_text("\n", strip=False) if hasattr(element, "get_text") else str(element)
            if text:
                lines.append(_inline(text))
        body = "\n".join(lines)
        # 去掉签名行与多余空行
        body = _SIGNATURE_RE.sub("", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body.strip(), images

__all__ = ["PttAPI", "PttArticle", "PttPush", "USER_AGENT"]
