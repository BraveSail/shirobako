"""富文本 blocks 转换器的离线用例 (纯函数, 不联网/不发送)。"""

from pyrogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
)
from pyrogram.types.messages_and_media.rich_text import (
    RichTextBold,
    RichTextCode,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextUrl,
)

from plugins.parse.rich_blocks import markdown_to_blocks, parse_inline


def test_parse_inline_plain_text():
    assert parse_inline("普通文字") == "普通文字"


def test_parse_inline_bold():
    parts = parse_inline("前**粗**后")
    assert isinstance(parts[1], RichTextBold)


def test_parse_inline_markdown_link():
    """[文字](url)"""
    parts = parse_inline("看 [文字](https://a.com)")
    link = next(p for p in parts if isinstance(p, RichTextUrl))
    assert link.text == "文字"
    assert link.url == "https://a.com"


def test_parse_inline_html_link():
    """footer 里的来源链接是 HTML: markdown 链接语法在 footer 块里不解析"""
    parts = parse_inline('14:37 · 32,846 查看 · <a href="https://x.com/u/status/1">来源（Twitter）</a>')
    link = next(p for p in parts if isinstance(p, RichTextUrl))
    assert link.text == "来源（Twitter）"
    assert link.url == "https://x.com/u/status/1"


def test_parse_inline_html_link_unescapes_entities():
    parts = parse_inline('<a href="https://c.com?a=1&amp;b=2">x &amp; y</a>')
    link = next(p for p in parts if isinstance(p, RichTextUrl))
    assert link.url == "https://c.com?a=1&b=2"
    assert link.text == "x & y"


def test_literal_escaped_pipes_do_not_split_an_anchor_with_pipes_in_its_label():
    parts = parse_inline(r'\|\| あらすじ&amp; <a href="https://a.com/a">表記||</a>')
    assert isinstance(parts, list)
    prefix, link = parts
    assert prefix == "|| あらすじ& "
    assert isinstance(link, RichTextUrl)
    assert link.text == "表記||"
    assert link.url == "https://a.com/a"


def test_unescaped_spoiler_can_still_wrap_a_native_link():
    parts = parse_inline('||<a href="https://a.com/a">隠しリンク</a>||')
    assert isinstance(parts, list)
    (spoiler,) = parts
    assert isinstance(spoiler, RichTextSpoiler)
    assert isinstance(spoiler.text[0], RichTextUrl)


def test_html_entities_in_link_labels_do_not_create_markup():
    parts = parse_inline('<a href="https://a.com/a">&#124;&#124;&lt;b&gt;字&lt;/b&gt;&#124;&#124;</a>')
    assert isinstance(parts, list)
    (link,) = parts
    assert isinstance(link, RichTextUrl)
    assert link.text == "||<b>字</b>||"


def test_escaped_markdown_delimiters_remain_literal_even_next_to_a_real_link():
    parts = parse_inline(r'\*xx\* \`xx\` \[xx\]\(xx\) <a href="https://a.com/a">link</a>')
    assert parts[0] == "*xx* `xx` [xx](xx) "
    assert isinstance(parts[1], RichTextUrl)


def test_parse_inline_code_and_strikethrough():
    assert isinstance(parse_inline("`码`")[0], RichTextCode)
    assert isinstance(parse_inline("~~删~~")[0], RichTextStrikethrough)


def test_blocks_heading_and_divider_and_footer():
    blocks = markdown_to_blocks("### 标题\n\n---\n\n<footer>页尾</footer>")
    assert isinstance(blocks[0], InputRichBlockSectionHeading)
    assert blocks[0].size == 3
    assert isinstance(blocks[1], InputRichBlockDivider)
    assert isinstance(blocks[2], InputRichBlockFooter)


def test_blocks_consecutive_lines_stay_one_paragraph():
    """连续非空行必须合并成一个段落, 不能一行一个块"""
    blocks = markdown_to_blocks("第一行\n第二行\n第三行")
    assert len(blocks) == 1
    assert isinstance(blocks[0], InputRichBlockParagraph)


def test_blocks_quote_and_list():
    blocks = markdown_to_blocks("> 引用一\n> 引用二\n\n- 项一\n- 项二")
    assert isinstance(blocks[0], InputRichBlockBlockQuotation)
    assert isinstance(blocks[1], InputRichBlockList)
    assert len(blocks[1].items) == 2


def test_a_poll_table_inside_a_quote_becomes_a_table_block():
    """引用块里的 markdown 表格要转成 ``Table`` 块 —— 以前行被合并成段落,
    投票表格在敏感内容里散架（用户报「引用里的投票也格式化一下」）。"""
    quoted = (
        "> <i>作者</i>\n>\n> ---\n>\n> <i>正文</i>\n> | 选项 | 票数 | 占比 |\n> | --- | --- | --- |\n> | A | 3 | 75% |"
    )
    blocks = markdown_to_blocks(quoted)
    quote = blocks[0]
    assert isinstance(quote, InputRichBlockBlockQuotation)
    tables = [b for b in quote.blocks if isinstance(b, InputRichBlockTable)]
    assert tables, [type(b).__name__ for b in quote.blocks]
    rows = [[c.text for c in row] for row in tables[0].cells]
    assert rows == [["选项", "票数", "占比"], ["A", "3", "75%"]], rows


def test_a_pipe_line_without_a_separator_stays_text():
    """孤零零一行 ``| x |`` 不算表格 (markdown 规矩) —— 不能转出 Table 块"""
    blocks = markdown_to_blocks("> <i>作者</i>\n> | x |")
    quote = blocks[0]
    assert not [b for b in quote.blocks if isinstance(b, InputRichBlockTable)]


def test_blocks_checkbox_list():
    blocks = markdown_to_blocks("- [x] 已完成\n- [ ] 未完成")
    items = blocks[0].items
    assert items[0].has_checkbox is True and items[0].is_checked is True
    assert items[1].is_checked is False


def test_blocks_preformatted_keeps_code():
    blocks = markdown_to_blocks("```python\nprint(1)\n```")
    assert isinstance(blocks[0], InputRichBlockPreformatted)
    assert "print(1)" in blocks[0].text
    assert blocks[0].language == "python"


def test_blocks_media_placeholder_uses_given_block():
    blocks = markdown_to_blocks("正文\n\n![](tg://photo?id=m0)\n\n后文", media_blocks={"m0": "MEDIA"})
    assert blocks[1] == "MEDIA"


def test_blocks_collage_groups_multiple_media():
    """<tg-collage> 包住的多个媒体要变成一个 Collage 块"""
    from pyrogram.types import InputRichBlockCollage

    md = "正文\n\n<tg-collage>\n\n![](tg://photo?id=m0)\n![](tg://photo?id=m1)\n\n</tg-collage>"
    blocks = markdown_to_blocks(md, media_blocks={"m0": "M0", "m1": "M1"})
    assert isinstance(blocks[1], InputRichBlockCollage)
    assert blocks[1].blocks == ["M0", "M1"]


def test_blocks_collage_with_single_media_flattens():
    """只有一张时不套 Collage (没必要)"""
    md = "<tg-collage>\n\n![](tg://photo?id=m0)\n\n</tg-collage>"
    blocks = markdown_to_blocks(md, media_blocks={"m0": "M0"})
    assert blocks == ["M0"]


def test_blocks_unknown_media_placeholder_is_skipped():
    """占位没有对应媒体块时跳过, 不能把 tg:// 链接漏给用户看"""
    blocks = markdown_to_blocks("正文\n\n![](tg://photo?id=nope)")
    assert len(blocks) == 1
