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


class PttParser(BaseParser):
    __platform__ = Platform.PTT
    __supported_type__ = ["图文"]
    __match__ = r"^(https?://)?(www\.)?ptt\.cc/bbs/.+\.html"

    async def _do_parse(self, raw_url: str) -> RichTextParseResult:
        article = await PttAPI(proxy=self.proxy).fetch(raw_url)
        return RichTextParseResult(
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
            # 行间留空行，服务端才会切成独立的 blockquote —— 连续 > 行会被合并成
            # 一个大框，那就不是"每条一个引用"了。
            #
            # ⚠️ 必须按预算截断: 这条帖子有 655 条推文，每条一个框渲染后 39K 字符，
            # 超过富文本 32768 上限（整条会被服务端拒掉）。预算 = 总量减正文，
            # 末尾注明还剩多少条。
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
                lines.append("")
            parts.append("\n".join(lines).rstrip())
        return "\n\n".join(parts)


__all__ = ["PttParser"]
