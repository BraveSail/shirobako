"""引用块里的**硬换行**：行尾两空格必须留在 ``<i>`` 外面。

markdown 的硬换行是"行尾两个空格 + 换行"。``format_quote_block`` 给整行包 ``<i>…</i>``
做斜体 —— 如果那两空格被包进标签里，行尾字符变成 ``</i>``，服务端不再当硬换行，
**多行会被并成一行**。

真机实测（读回服务端块结构）::

    > <i>行1  </i>      ← 两空格在标签内 → Paragraph[1 行] ❌
    > <i>行2</i>

    > <i>行1</i>        ← 两空格在标签外 → Paragraph[2 行] ✓
    > <i>行2</i>

踩到的场景：bgm 章节页的主楼是官方章节信息（``div.epDesc``：时长/首播 + 简介 + STAFF），
它靠 ``<br/>`` 分行 —— 标记被包进标签里的话整段会被并成一行。
"""

from parsehub.utils.helpers import _quote_line, format_quote_block

# ── 单行 ───────────────────────────────────────────────────────────────

def test_a_plain_line_is_italicised():
    assert _quote_line("正文") == "> <i>正文</i>"


def test_a_hard_break_marker_stays_outside_the_italic_tag():
    """**核心**: 行尾两空格留在 ``</i>`` **之后**"""
    assert _quote_line("行1  ") == "> <i>行1</i>  "


def test_a_blank_line_stays_a_bare_marker():
    """空行仍是光秃秃的 ``>``（引用块靠它分段）"""
    assert _quote_line("") == ">"
    assert _quote_line("   ") == ">"


def test_the_marker_is_kept_verbatim():
    """行尾空白原样保留（markdownify 的 br 给的是两空格，但别假定恰好两格）"""
    assert _quote_line("行  ") == "> <i>行</i>  "
    assert _quote_line("行 ") == "> <i>行</i> "


# ── poll 占位符行 ────────────────────────────────────────────────────────


def test_a_poll_placeholder_line_is_not_italicised():
    """**核心**: 引用块里的 ``{{poll:N}}`` 行要原样输出 —— 渲染层按整行替换成
    本地化表格, 残留的 ``<i>`` 会污染替换产物 (与表格行同理)。

    用户场景: 被引用/被回复的推文、楼层带投票时, 占位符就落在这个引用块里。
    """
    assert _quote_line("{{poll:0}}") == "> {{poll:0}}"
    assert _quote_line("{{poll:12}}") == "> {{poll:12}}"
    # 带行尾空白 (markdownify 的 br) 也照旧不带硬换行标记
    assert _quote_line("{{poll:0}}  ") == "> {{poll:0}}"


def test_a_block_with_a_poll_placeholder_keeps_the_line_bare():
    """整块渲染: 占位符行不带斜体, 其它行照旧带"""
    block = format_quote_block("正文\n{{poll:0}}")
    assert block == "> <i>正文</i>\n> {{poll:0}}\n\n", block
    assert "<i>{{poll" not in block, block


def test_a_bare_poll_like_text_is_still_italicised():
    """自检: 只有**独立一行**的占位符才豁免 —— 夹在文字里的是普通文本, 照旧斜体。"""
    assert _quote_line("见 {{poll:0}} 处") == "> <i>见 {{poll:0}} 处</i>"
    assert _quote_line("{{poll:x}}") == "> <i>{{poll:x}}</i>"


# ── 整块 ───────────────────────────────────────────────────────────────

def test_the_block_keeps_every_line_separate():
    """整块渲染：每一行的标记都在标签外，行数不丢"""
    block = format_quote_block("行1  \n行2  \n行3")
    lines = block.rstrip("\n").splitlines()
    assert lines == ["> <i>行1</i>  ", "> <i>行2</i>  ", "> <i>行3</i>"], lines


def test_the_author_line_has_no_trailing_marker():
    """署名行不带硬换行标记（它不是正文行）"""
    block = format_quote_block("正文", "作者")
    assert block.splitlines()[0] == "> <i>作者</i>"


def test_a_block_without_markers_is_unchanged():
    """**回归**: 没有硬换行的正文逐字不变"""
    assert format_quote_block("单行正文") == "> <i>单行正文</i>\n\n"
    assert format_quote_block("两段\n\n二") == "> <i>两段</i>\n>\n> <i>二</i>\n\n"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
