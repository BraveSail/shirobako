"""正文里的 ``#标签`` 一律自己链接化 —— 不靠服务端识别。

背景（2026-10-06, 用户报「为什么有的 tag 是超链接有的不是」）: 标签有两个来源 ——
**标签行**（`format_tags`，我们手动写成链接）与**正文里的裸标签**（靠服务端 hashtag
自动识别）。而服务端的识别有字符限制（实测）:

| 标签 | 服务端识别 | 说明 |
| --- | --- | --- |
| `#INMUKING` / `#アニメ` / `#foo_bar` | ✅ | 正常 |
| `#foo-bar` | ⚠️ 只识别 `#foo` | 连字符处**截断**，看着像标签被切了 |
| `#foo.bar` | ⚠️ 只识别 `#foo` | 点处截断 |
| `#123` | ❌ 完全不识别 | 纯数字标签不可点 |

⇒ 同一条消息里就出现"有的标签是链接、有的不是"。现在正文标签也由我们自己写成
`<a href="平台标签页">`，与标签行同一形态。
"""

from parsehub.types import Platform

from plugins.helpers import link_hashtags


def test_a_tag_in_the_middle_of_a_line_is_linked():
    out = link_hashtags("GPU KING #INMUKING", platform=Platform.TWITTER)
    assert '<a href="https://x.com/hashtag/INMUKING">#INMUKING</a>' in out


def test_a_non_ascii_tag_is_linked_and_url_encoded():
    """日文标签: 服务端能识别, 但我们照样显式链接 (并且 URL 要编码)"""
    out = link_hashtags("作品 #INMUKINGの刑", platform=Platform.TWITTER)
    assert "<a href=" in out
    assert "%E3%81%AE" in out, out          # の 的 UTF-8 百分号编码
    assert ">#INMUKINGの刑</a>" in out


def test_a_hyphenated_tag_is_linked_whole():
    """`#foo-bar` 服务端只认到 `#foo` —— 我们链接整串"""
    out = link_hashtags("#foo-bar", platform=Platform.TWITTER)
    assert ">#foo-bar</a>" in out
    assert "hashtag/foo-bar" in out


def test_a_numeric_tag_is_left_as_text():
    """纯数字**不链接**：那通常是楼层号/序号（``作者 · #1``），不是标签。

    服务端本来也不把 ``#123`` 识别成 hashtag；硬链接它反而会造出指向不存在标签页的
    链接（并曾把 ``#1</i>`` 的闭合标签吞进链接文字，导致正文裸露 ``<i>``）。
    """
    out = link_hashtags("见 #123", platform=Platform.TWITTER)
    assert "#123" in out
    assert "<a href=" not in out, out


def test_a_trailing_period_stays_outside_the_tag():
    """句读是边界: `#tag.` 的句号不属于标签"""
    out = link_hashtags("看这个 #tag.", platform=Platform.TWITTER)
    assert ">#tag</a>." in out


def test_a_tag_does_not_swallow_a_following_html_tag():
    """标签后面紧跟的 HTML 标签**不能被吞进标签名**。

    实测症状（linux.do 引用块署名行 ``作者 · #1``）: 标签正则不排除 ``<`` ``>`` ``/``，
    于是 ``#1</i>`` 整段被当成标签名 —— ``</i>`` 变成链接文字、闭合标签丢失，
    后面所有 ``<i>`` 就裸露在消息里（用户报「引用里为什么有个 i 标签」）。
    """
    out = link_hashtags("> <i>SUN · #1</i>", platform=Platform.TWITTER)
    assert "</i>" in out, f"闭合标签必须保留: {out!r}"
    assert "&lt;/i&gt;" not in out and "&amp;lt;/i&amp;gt;" not in out, out
    assert out.count("<i>") == out.count("</i>"), out


def test_a_pure_number_is_not_a_tag():
    """纯数字是楼层号/序号，不是标签（linux.do 的 ``作者 · #1``）"""
    out = link_hashtags("Herta42 · #1", platform=Platform.LINUXDO)
    assert "#1" in out
    assert "<a href=" not in out, f"#1 不该被链接: {out!r}"


# ── 平台实体优先：精确边界（用户报的 `#FX戦士くるみちゃん」第一話より`） ──


def test_the_entity_fixes_the_japanese_bracket_boundary():
    """**核心**: 实体给了精确标签名，日文 `」` 不再被吃进标签。

    用户报障原话: tag 识别成 `#FX戦士くるみちゃん」第一話より` 了。
    正则的终止集合没枚举 `」` ⇒ 一路吃到空格；而 API 的
    `entities.hashtags[].text` 就是 `FX戦士くるみちゃん`（与网页 hashtag 链接一致）。
    """
    out = link_hashtags(
        "TVアニメ「#FX戦士くるみちゃん」第一話より", Platform.TWITTER, ["FX戦士くるみちゃん"]
    )
    assert '">#FX戦士くるみちゃん</a>」第一話より' in out, out
    # 链接文字里不能再出现 `」`
    assert "」第一話より</a>" not in out, out


def test_without_the_entity_the_regex_still_runs():
    """没实体时仍兜底，但周围句读不是标签的一部分。"""
    out = link_hashtags("TVアニメ「#FX戦士くるみちゃん」第一話より", Platform.TWITTER)
    assert "#FX戦士くるみちゃん</a>」第一話より" in out, out


def test_episode_number_and_decorative_pipes_are_not_an_inferred_hashtag():
    src = "||◤ #08「作戦」◢|| #foo-bar||"
    out = link_hashtags(src, Platform.TWITTER)
    assert out.startswith("||◤ #08「作戦」◢|| ")
    assert out.count("<a ") == 1
    assert out.endswith('"https://x.com/hashtag/foo-bar">#foo-bar</a>||')


def test_combining_marks_belong_to_their_tag_words():
    src = "「#हिन्दी」 #Cafe\u0301||"
    out = link_hashtags(src, Platform.TWITTER)
    assert ">#हिन्दी</a>」" in out
    assert out.endswith(">#Cafe\u0301</a>||")


def test_the_longest_name_wins():
    """`#foo` 与 `#foobar` 都在实体里时，先换长的，避免换出残链 `<a>#foo</a>bar`"""
    out = link_hashtags("#foobar 和 #foo", Platform.TWITTER, ["foo", "foobar"])
    assert '<a href="https://x.com/hashtag/foobar">#foobar</a>' in out, out
    assert '<a href="https://x.com/hashtag/foo">#foo</a>' in out, out
    assert "</a>bar" not in out, out


def test_regex_metacharacters_in_the_name_are_escaped():
    """实体名可能含正则元字符 —— 必须 re.escape，否则要么匹配不上要么匹配错"""
    out = link_hashtags("#a.c 结束", Platform.TWITTER, ["a.c"])
    assert '#a.c</a>' in out, out


def test_an_entity_name_that_is_not_in_the_text_changes_nothing():
    """实体与正文不一致（被平台改写过）时**不动正文**，交给兜底正则"""
    src = "正文里只有 #other"
    out = link_hashtags(src, Platform.TWITTER, ["不存在的标签"])
    assert out == link_hashtags(src, Platform.TWITTER), out


def test_entities_do_not_touch_text_inside_an_existing_anchor():
    """已有锚点里的内容不二次包装（与正则那条路同一纪律）"""
    src = '<a href="https://x.com/hashtag/foo">#foo</a> 以及 #foo'
    out = link_hashtags(src, Platform.TWITTER, ["foo"])
    assert out.count("<a ") == 2, out


def test_a_fragment_in_a_url_is_not_a_tag():
    """URL 里的 `#片段` 不是标签 (前面是字母/斜杠)"""
    out = link_hashtags("https://x.com/a#b", platform=Platform.TWITTER)
    assert "<a href=" not in out


def test_an_existing_anchor_is_not_wrapped_again():
    """已经是链接的整段跳过 (锚点内的文字不再包第二层)"""
    src = '<a href="https://x.com/hashtag/x">#x</a>'
    assert link_hashtags(src, platform=Platform.TWITTER) == src


def test_a_platform_without_a_tag_page_falls_back_to_text():
    """平台没有标签页模板: 退回纯文本; 行首那个 `#` 要转义 (否则变一级标题)"""
    out = link_hashtags("#tag 在行首", platform=None)
    assert out.startswith("\\#tag"), out
    mid = link_hashtags("正文 #tag 在行中", platform=None)
    assert "#tag" in mid and "<a href=" not in mid
    assert "\\#" not in mid, "行中的 # 不必转义"


def test_the_body_tag_ends_up_linked_in_a_full_render():
    """端到端: 正文里的标签在完整渲染里是链接 (这次修的就是它)"""
    import types

    from plugins.helpers import build_rich_markdown

    result = types.SimpleNamespace(
        title="", content="GPU KING #INMUKINGの刑", raw_url="https://x.com/a/status/1",
        author_name="A", author_handle="a", author_url="", published_at=None,
        view_count=None, like_count=None, tags=None, platform=Platform.TWITTER, media=None,
    )
    config = types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=True)
    md = build_rich_markdown(result, config=config, lang="zh-hans")
    assert '<a href="https://x.com/hashtag/' in md, md
    assert ">#INMUKINGの刑</a>" in md


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
