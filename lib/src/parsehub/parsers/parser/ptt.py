"""PTT（批踢踢實業坊）文章解析。

页面是纯 HTML（无 API），正文是纯文本 + 链接，推文在 ``div.push`` 里 ——
结构与字段见 ``provider_api/ptt.py`` 顶部说明。
"""

from __future__ import annotations

from ...provider_api.ptt import PttAPI, PttArticle
from ...types import ImageRef, Platform, RichTextParseResult
from ...utils.helpers import image_ext_from_url
from ..base.base import BaseParser

#: 富文本正文上限（服务端限制），推文截断预算按它算
_TOTAL_BUDGET = 31_000
#: 推文段至少保留这么多字符（正文特别长时不要一条都不给）
_PUSH_MIN_BUDGET = 3_000
#: 单条推文的**渲染开销**（源 ~20 字符 → 服务端 ``<blockquote>…</blockquote>`` 约 50）
_PUSH_RENDER_COST = 50
#: 推文条数硬上限（再多的部分只给提示）
_MAX_PUSHES = 400


class PttRichTextParseResult(RichTextParseResult):
    """PTT 文章。

    ``<img>`` 图被抽成 media、原位留 ``{{media:N}}``，因此
    ``requires_media_download`` 必须为 True —— 否则流水线走「富文本跳过下载」，
    占位符没有对应媒体就被渲染层删掉，图静默消失（用户实测「还是没图」）。
    """

    requires_media_download = True


class PttParser(BaseParser):
    __platform__ = Platform.PTT
    __supported_type__ = ["图文"]
    __match__ = r"^(https?://)?(www\.)?ptt\.cc/bbs/.+\.html"

    async def _do_parse(self, raw_url: str) -> PttRichTextParseResult:
        article = await PttAPI(proxy=self.proxy).fetch(raw_url)
        return PttRichTextParseResult(
            title=article.title,
            markdown_content=self._render(article),
            author_name=article.author_name,
            author_handle=article.author_handle,
            # PTT 没有个人主页，作者就只显示名字（不拼链接）
            media=[
                ImageRef(url=url, thumb_url=url, ext=image_ext_from_url(url))
                for url in article.images
            ],
            published_at=article.published_at,
            # 页脚的统计位: PTT 没有浏览/点赞，但推文数有意义（"N 条推文"）
            reply_count=len(article.pushes) or None,
            tags=[article.board] if article.board else None,
        )

    @staticmethod
    def _render(article: PttArticle) -> str:
        """正文 + 推文。

        推文保留 PTT 原生的 ``推 user: 内容`` 形态（读者认得这个格式）；条数可能
        几百条，交给 ``format_text`` 的整段折叠处理，不在这里截断。
        """
        parts: list[str] = []
        if article.body:
            parts.append(article.body)
        if article.pushes:
            # 每条推文**各自一个引用块**（用户明确要求「每条回复每个引用」）。
            # 行间留空行，服务端才会切成独立的 blockquote。
            #
            # ⚠️ **整段必须再包一层 <details> 收起**：几百条引用会让消息非常高，
            # Telegram **客户端**会对超长消息自动折叠（"显示更多"），而那层折叠
            # **会把末尾的页脚一起盖住**（用户报「页脚在显示更多里面」）。
            # 这与我们自己的「展开全文」是两回事：那是 <details>（我们可控），
            # 「显示更多」是客户端行为（不可控，只能让消息变矮）。
            #
            # 条数也有上限: 富文本正文 32768 字符上限，每条引用渲染约 50 字符。
            budget = max(_PUSH_MIN_BUDGET, _TOTAL_BUDGET - len(article.body))
            max_pushes = min(_MAX_PUSHES, budget // _PUSH_RENDER_COST)
            lines: list[str] = []
            used = 0
            shown = 0
            for push in article.pushes:
                tag = push.tag or "→"
                line = f"> {tag} {push.user}: {push.content}".rstrip()
                if shown >= max_pushes or (shown and used + len(line) + 2 > budget):
                    break
                lines.append(line)
                lines.append("")
                used += len(line) + 2
                shown += 1
            remaining = len(article.pushes) - shown
            if remaining > 0:
                lines.append(f"> …還有 {remaining} 條推文未顯示")
            summary = f"推文（{len(article.pushes)} 條）"
            parts.append(f"<details><summary>{summary}</summary>\n\n" + "\n".join(lines).rstrip() + "\n\n</details>")
        return "\n\n".join(parts)


__all__ = ["PttParser"]
