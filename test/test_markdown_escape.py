"""Telegram 保留字符转义（escape_telegram_markdown）。

背景（2026-10-22 用户报「为什么识别打码」）：X 的一条动画预告正文就是
``||◤ 先行カット＆あらすじ公開 ◢||`` —— Telegram 把 ``||`` 当 spoiler 语法，
整行被打码；而 ``possibly_sensitive`` 其实是 False，跟敏感判定无关。

官方文档给的保留字符清单（MarkdownV2 同族）：
``_ * [ ] ( ) ~ ` > # + - = | { } . !``

不能无脑全转 —— 其中 ``* _ ` > [ ] ( )`` 是项目自己生成语法用的，全转会把
加粗 / 引用 / 链接 / 代码一起打坏。所以「保护语法块 + 转义其余」。
"""

from plugins.helpers import escape_telegram_markdown as esc


def test_spoiler_pipes_in_plain_text_are_escaped():
    """原文的 ||…|| 必须转义，否则服务端渲染成打码"""
    out = esc("||◤ 先行カット＆あらすじ公開 ◢||")
    assert out == r"\|\|◤ 先行カット＆あらすじ公開 ◢\|\|"


def test_our_own_markup_is_preserved():
    """我们自己生成的语法块一个都不能被转义"""
    src = (
        "**粗体** 与 ***粗斜体*** 与 <i>斜体</i>\n\n"
        "> 引用行\n\n"
        "[链接](https://x.com/a_b)\n\n"
        "`inline code`\n\n"
        "```python\nprint('x')\n```\n\n"
        "{{media:0}} {{poll:1}}"
    )
    out = esc(src)
    for chunk in (
        "**粗体**",
        "***粗斜体***",
        "<i>斜体</i>",
        "> 引用行",
        "[链接](https://x.com/a_b)",
        "`inline code`",
        "```python\nprint('x')\n```",
        "{{media:0}}",
        "{{poll:1}}",
    ):
        assert chunk in out, (chunk, out)


def test_bare_url_keeps_its_underscores():
    """裸 URL 里的 _ 不能被转义（会把链接打断）"""
    out = esc("see https://example.com/a__b__c/page for details")
    assert "https://example.com/a__b__c/page" in out


def test_plain_reserved_characters_are_escaped():
    """普通文本里的保留字符按官方清单转义"""
    out = esc("1+1=2, v1.0.0! {x} ~y~ a_b")
    for ch in ("+", "=", ".", "!", "{", "}", "~", "_"):
        assert f"\\{ch}" in out, (ch, out)


def test_table_rows_survive_but_spoiler_pipes_do_not():
    """表格行（| a | b |）原样保留；||文字|| 不是表格，要转义"""
    assert esc("| a | b |\n|---|---|") == "| a | b |\n|---|---|"
    assert esc("||文字||") == r"\|\|文字\|\|"


def test_hashtag_anchor_keeps_its_inner_text_intact():
    """<a> 里的可见文字（#tag）不能被转义，否则服务端不认锚点"""
    anchor = '<a href="https://x.com/hashtag/T">#T</a>'
    assert esc(anchor) == anchor


def test_leading_indent_is_left_alone_for_the_blocks_path():
    """行首缩进**原样保留** —— 富文本走 blocks 结构化通道，段落文本是字面量。

    历史：markdown 字符串通道会把行首 4+ 空格解析成缩进代码块，于是加过一版
    「换成全角空格」的补丁；那版把缩进宽度翻倍、把平台用半角空格做的对齐推歪
    （用户报「拆东墙补西墙」）。改成 blocks 通道后这个补丁整个删掉了 ——
    原文的缩进不需要也不应该被改写。
    """
    out = esc("━━━\n\n      TVアニメ『あかね噺』\n  第２期キービジュアル 公開")
    # 6 个半角空格原样保留，一个都没被换掉
    assert "      TVアニメ『あかね噺』" in out
    assert "\u3000" not in out
    # 少于 4 个空格同样不动
    assert "  第２期キービジュアル 公開" in out
