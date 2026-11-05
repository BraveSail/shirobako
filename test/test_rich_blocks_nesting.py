"""blocks 通道的两个渲染坑：嵌套折叠、markdown 转义残留。

两个都是「把生成的语法再当文本解析」的产物：

1. **嵌套折叠**：PTT 帖子把推文列表折成「推文（261 條）」，里面又套一个「展开全文」。
   旧实现找**第一个** ``</details>`` 就判定结束，内层一闭合就截断 —— 内层闭合标签
   和外层剩余内容全部漏成正文段落，用户看到字面的 ``</details>``。
2. **转义残留**：``format_text`` 给平台原文加 ``\\#`` 这类反斜杠转义（markdown 字符串
   通道会还原它）。blocks 通道把文本当**字面量**，没人还原 ⇒ 用户看到 ``\\#C_Chat``。
"""
from pyrogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockParagraph,
)

from plugins.parse.rich_blocks import markdown_to_blocks, parse_inline


def _flat_text(blocks) -> str:
    """拼出所有块的文字（含嵌套）。"""
    out: list[str] = []

    def walk(nodes):
        for node in nodes or []:
            text = getattr(node, "text", None)
            if isinstance(text, str):
                out.append(text)
            elif isinstance(text, list):
                out.append("".join(x if isinstance(x, str) else getattr(x, "text", "") for x in text))
            for attr in ("blocks", "items"):
                child = getattr(node, attr, None)
                if isinstance(child, list):
                    walk(child)

    walk(blocks)
    return "\n".join(out)


def test_nested_details_pair_by_depth():
    """嵌套折叠必须按深度配对，内层闭合不能结束外层。"""
    md = (
        "<details><summary>推文（3 條）</summary>\n\n"
        "> 推 a: 一\n\n"
        "<details><summary>展开全文</summary>\n\n"
        "> 推 b: 二\n\n"
        "</details>\n\n"
        "</details>\n\n"
        "尾部文字"
    )
    blocks = markdown_to_blocks(md)

    # 顶层: Details + 尾部段落 —— 闭合标签不能漏成段落
    assert isinstance(blocks[0], InputRichBlockDetails)
    assert isinstance(blocks[-1], InputRichBlockParagraph)
    assert _flat_text([blocks[-1]]) == "尾部文字"
    assert "</details>" not in _flat_text(blocks)

    # 外层 Details 里含引用块和内层 Details
    outer = blocks[0].blocks
    assert any(isinstance(b, InputRichBlockBlockQuotation) for b in outer)
    inner = [b for b in outer if isinstance(b, InputRichBlockDetails)]
    assert len(inner) == 1, "内层折叠要保留成嵌套块"
    assert any(isinstance(b, InputRichBlockBlockQuotation) for b in inner[0].blocks)


def test_single_line_details_still_works():
    """单行折叠块（整个 <details>…</details> 在一行）不能被当成嵌套。"""
    blocks = markdown_to_blocks("<details><summary>展开</summary>正文</details>")
    assert len(blocks) == 1
    assert isinstance(blocks[0], InputRichBlockDetails)
    assert "正文" in _flat_text(blocks)


def test_markdown_escapes_are_restored_in_blocks():
    """``\\#`` 这类转义要还原成字面字符 —— blocks 通道没人替我们解转义。"""
    assert parse_inline("\\#C_Chat") == "#C_Chat"
    blocks = markdown_to_blocks("\\#C_Chat")
    assert _flat_text(blocks) == "#C_Chat", f"实际: {_flat_text(blocks)!r}"


def test_escaped_hashtag_does_not_become_a_link():
    """还原后的 ``#tag`` 是纯文本 —— 不能被当成话题链接。"""
    blocks = markdown_to_blocks("\\#C_Chat")
    text = blocks[0].text
    assert isinstance(text, str), f"不应生成链接片段: {text!r}"


def test_all_official_reserved_characters_round_trip():
    """官方 18 个保留字符的转义都要能还原。"""
    reserved = "_*[]()~`>#+-=|{}.!"
    escaped = "".join(f"\\{ch}" for ch in reserved)
    assert parse_inline(escaped) == reserved
