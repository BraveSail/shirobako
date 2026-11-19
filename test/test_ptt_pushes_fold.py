"""PTT 推文段必须**独立于正文折叠**。

## 用户报的问题

> 这个把推文不要放进正文折叠里，分出来

PTT 的推文段本来已经包在自己的 ``<details><summary>推文（N 條）</summary>`` 里，
但整篇正文超阈值时 ``fold_for_show_more`` 会再套一层 ``<details>展开全文<details>``。
结果：用户点开「展开全文」，看到的第一个东西**又是一个要点的「推文（N 條）」** ——
推文实质上仍被关在正文折叠里，而且折叠判断把推文长度也算进了正文。

## 契约

1. 正文该折就折（长正文仍然要收起来，页脚不能被「显示更多」盖住）；
2. **推文段留在「展开全文」外面**，与正文折叠平级；
3. 正文本身不超阈值、只是被推文撑超时 —— **不折**（推文已自己折好）；
4. 手动打码（``hide_content``）不受影响：那时整组内容本来就该一起进 details。
"""

from __future__ import annotations

import pytest

from plugins.helpers import (
    SHOW_MORE_VISIBLE_LIMIT,
    fold_for_show_more,
    split_trailing_details,
    visible_length,
)

# 一条推文的渲染形态（与 ptt.py 的 _render 一致）
PUSH = "> 推 gsock: 6"


def _pushes(n: int) -> str:
    """n 条推文，每条后面一个空行，整体包在 details 里（PTT 的真实形态）。"""
    lines: list[str] = []
    for i in range(n):
        lines.append(f"> 推 user{i}: 這是第 {i} 條推文內容")
        lines.append("")
    body = "\n".join(lines).rstrip()
    return f"<details><summary>推文（{n} 條）</summary>\n\n{body}\n\n</details>"


class TestSplitTrailingDetails:
    def test_splits_trailing_details(self) -> None:
        content = "正文內容\n\n" + _pushes(3)
        body, tail = split_trailing_details(content)
        assert body == "正文內容"
        assert tail.startswith("<details>")
        assert "推文（3 條）" in tail

    def test_no_details_returns_unchanged(self) -> None:
        content = "只有正文，沒有推文"
        body, tail = split_trailing_details(content)
        assert body == content
        assert tail == ""

    def test_middle_details_is_not_split(self) -> None:
        """正文中间的 details（打码/图集）不该被当成尾部块拆走。"""
        content = "<details><summary>遮</summary>\n\n秘密\n\n</details>\n\n後面還有正文"
        body, tail = split_trailing_details(content)
        assert body == content
        assert tail == ""

    def test_whole_content_is_one_details(self) -> None:
        """整篇就是一个 details 时不拆 —— 拆了正文就空了。"""
        content = _pushes(2)
        body, tail = split_trailing_details(content)
        assert body == content
        assert tail == ""

    def test_nested_details_pairs_by_depth(self) -> None:
        """嵌套时只认最外层开标签，不能截在内层。"""
        inner = "<details><summary>內層</summary>\n\nx\n\n</details>"
        content = f"正文\n\n<details><summary>推文（1 條）</summary>\n\n{inner}\n\n</details>"
        body, tail = split_trailing_details(content)
        assert body == "正文"
        assert tail.count("<details>") == 2
        assert tail.count("</details>") == 2


class TestFoldKeepsPushesOut:
    def test_pushes_stay_outside_the_fold(self) -> None:
        """★ 核心：正文超阈值时，推文段必须在「展开全文」外面。"""
        long_body = "正文" * 5000  # 10000 字符，远超阈值 6800
        content = f"{long_body}\n\n{_pushes(5)}"
        body, tail = split_trailing_details(content)
        # keep_out 的契约：tail 已经拼在 body 尾部（build_rich_markdown 就是这么用的）
        folded = fold_for_show_more(f"{body}\n\n{tail}", "展开全文", keep_out=tail)

        # 推文段在折叠闭合之后
        close = folded.rfind("</details>\n\n" + tail)
        assert close > 0, "推文段必须在正文折叠外面"
        # 两层 details 是对的：「展开全文」一层 + 推文自己一层（本来就有）
        assert folded.count("<details>") == 2
        assert tail in folded

    def test_short_body_streched_by_pushes_is_not_folded(self) -> None:
        """正文不长、只是被推文撑超阈值 ⇒ 不折（推文已经自己折好了）。"""
        body = "很短的正文"
        tail = "<details><summary>推文（500 條）</summary>\n\n" + ("> 推 x: y\n\n" * 500) + "</details>"
        folded = fold_for_show_more(f"{body}\n\n{tail}", "展开全文", keep_out=tail)
        # 只允许推文自己那层折叠，不该再套一层
        assert folded == f"{body}\n\n{tail}"
        assert folded.count("<details>") == 1

    def test_short_content_untouched(self) -> None:
        body = "短正文"
        assert fold_for_show_more(body, "展开全文", keep_out="") == body

    def test_without_keep_out_behaves_as_before(self) -> None:
        """不传 keep_out 时行为不变（其它平台不受影响）。"""
        long_body = "正文" * 5000
        folded = fold_for_show_more(long_body, "展开全文")
        assert folded.startswith("<details><summary>展开全文</summary>")
        assert long_body in folded

    def test_keep_out_not_found_falls_back(self) -> None:
        """keep_out 不在 body 里时退回整段折叠，不能吞内容。"""
        long_body = "正文" * 5000
        folded = fold_for_show_more(long_body, "展开全文", keep_out="不存在的段落")
        assert long_body in folded
        assert folded.startswith("<details>")


class TestVisibleLength:
    def test_details_counted_as_summary_only(self) -> None:
        """收起的 details 只算 summary 那行 —— 推文本身不占可见长度。"""
        tail = _pushes(100)
        assert visible_length(tail) < 100
        assert visible_length(tail) < len(tail)

    def test_footer_safe_after_refactor(self) -> None:
        """展开正文后，页脚不该再被客户端「显示更多」盖住。"""
        long_body = "正文" * 5000
        content = f"{long_body}\n\n{_pushes(300)}"
        body, tail = split_trailing_details(content)
        folded = fold_for_show_more(body, "展开全文", keep_out=tail)
        # 可见长度（折叠收起状态）应远小于阈值
        assert visible_length(folded) <= SHOW_MORE_VISIBLE_LIMIT


@pytest.mark.parametrize("n", [1, 10, 400])
def test_any_push_count_keeps_structure(n: int) -> None:
    content = f"正文\n\n{_pushes(n)}"
    body, tail = split_trailing_details(content)
    assert body == "正文"
    assert tail.count("<details>") == tail.count("</details>")
