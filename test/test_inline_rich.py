"""inline 富文本的媒体剥离、结果项构成与回答兜底 (离线, 不联网)。"""

import asyncio
from datetime import UTC, datetime

from parsehub.types import ImageRef, MultimediaParseResult, Platform, RichTextParseResult, VideoRef
from pyrogram.types import (
    InlineQueryResultArticle,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
)

from plugins.parse.inline import (
    _is_rich_result,
    build_inline_results,
    inline_cover_url,
    inline_reply_markup,
    strip_media_markdown,
)


def test_strip_media_markdown_removes_images():
    """inline 回答查询时不支持外部媒体, 正文里的图片语法必须剥掉"""
    assert strip_media_markdown("前\n\n![](https://a.com/x.jpg)\n\n后") == "前\n\n\n\n后"


def test_strip_media_markdown_keeps_text_and_links():
    md = "看 [文字](https://a.com) 与 **粗体**"
    assert strip_media_markdown(md) == md


def test_strip_media_markdown_handles_alt_text():
    assert strip_media_markdown("![封面](https://a.com/p.png) 正文").strip() == "正文"


def test_strip_media_markdown_handles_empty():
    assert strip_media_markdown("") == ""
    assert strip_media_markdown(None) == ""


def test_is_rich_result_detects_rich_article():
    rich = InlineQueryResultArticle(
        title="t", input_message_content=InputRichMessageContent(InputRichMessage(markdown="x"))
    )
    plain = InlineQueryResultArticle(title="t", input_message_content=InputTextMessageContent("x"))
    assert _is_rich_result(rich) is True
    assert _is_rich_result(plain) is False


# ── 结果项构成 ────────────────────────────────────────────────


def make_result(media=None, **kwargs):
    result = MultimediaParseResult(content="正文**粗体**", media=media, **kwargs)
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/u/status/1"
    result.published_at = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)
    result.view_count = 1455
    return result


# ── blocks 路径的折叠容器 ──────────────────────────────────────────────
#
# 与 <tg-time> 同一类坑: markdown 路径由**服务端**解析这两个折叠标签,
# blocks 路径必须自己认。不认的表现 (用户报的原文):
#   "<details><summary>⚠️ #不可以色色</summary>" 字面显示、内容散架,
#   **而且本该被折叠并打码的媒体掉到了折叠外面** —— 看起来就是"手动折叠时自动遮罩没工作"。


def test_blocks_path_parses_details():
    from pyrogram.types import InputRichBlockDetails

    from plugins.parse.rich_blocks import markdown_to_blocks

    md = "<details><summary>⚠️ #不可以色色</summary>\n\n被藏起来的正文\n\n</details>"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    node = blocks[0]
    assert isinstance(node, InputRichBlockDetails)
    # 摘要不能带上标签
    assert "不可以色色" in str(node.summary)
    assert "<summary>" not in str(node.summary)


def test_media_inside_details_stays_inside_and_keeps_its_spoiler():
    """**这条是用户报的那个 bug**: 媒体必须落在折叠块内, 且保留打码标记。

    以前不认 <details>, 占位符变成普通段落文字、媒体块被排到折叠**外面**。
    """
    from pyrogram.types import InputRichBlockDetails

    from plugins.parse.rich_blocks import SpoilerPhotoBlock, markdown_to_blocks

    md = "<details><summary>⚠️ #nsfw</summary>\n\n正文\n\n![](tg://photo?id=m0)\n\n</details>"
    blocks = markdown_to_blocks(md, media_blocks={"m0": SpoilerPhotoBlock(object())})
    details = [b for b in blocks if isinstance(b, InputRichBlockDetails)]
    assert details, "应该产出 details 块"
    inner = details[0].blocks
    assert any(isinstance(b, SpoilerPhotoBlock) for b in inner), (
        "媒体必须在折叠块内部 (掉到外面就等于没遮)"
    )
    assert not [b for b in blocks if isinstance(b, SpoilerPhotoBlock)], "折叠外不该再有媒体"


def test_blocks_path_parses_html_bold_and_italic():
    """footer 块只解析 HTML 标签, 所以阶段的 ``<b>▎…</b>`` 与引用块的 ``<i>``

    blocks 路径必须自己认 (不认就字面显示 "<b>"), 与 <tg-time> 同一类。
    """
    from pyrogram.types.messages_and_media.rich_text import RichTextBold, RichTextItalic

    from plugins.parse.rich_blocks import parse_inline

    bold = parse_inline("<b>▎上 传 中...</b> · 来源")
    assert any(isinstance(n, RichTextBold) for n in bold), "footer 里的 <b> 必须解析"

    italic = parse_inline("<i>@某人</i>")
    assert any(isinstance(n, RichTextItalic) for n in italic), "<i> 必须解析"

    # 不该把标签字面留在文本里
    assert "<b>" not in str(bold) and "<i>" not in str(italic)


def test_blocks_path_parses_expandable_quotation():
    from pyrogram.types import InputRichBlockExpandableBlockQuotation

    from plugins.parse.rich_blocks import markdown_to_blocks

    md = "<blockquote expandable><i>引用</i>第一行<br>第二行</blockquote>"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    assert isinstance(blocks[0], InputRichBlockExpandableBlockQuotation)
    # 块内 <br> 要变成真换行, 否则内容会挤成一行
    texts = [n for n in blocks[0].text if isinstance(n, str)]
    assert any("\n" in t for t in texts), f"<br> 没有转成真换行: {texts!r}"


def test_blocks_path_parses_the_time_entity():
    """blocks 的转换器必须认 ``<tg-time>`` —— markdown 路径由服务端解析它,

    blocks 路径得自己认。不认的话 footer 里那段会**原样**送出去, 用户看到字面的
    ``<tg-time unix=…>`` (敏感内容走 blocks, 所以只有敏感推文的页脚会这样)。
    """
    from pyrogram.types.messages_and_media.rich_text import RichTextDateTime

    from plugins.parse.rich_blocks import parse_inline

    tag = '<tg-time unix="1790855311" format="Dt">2026年10月1日 19:48</tg-time>'
    parts = parse_inline(f"{tag} · 138,460 查看")
    assert isinstance(parts, list), "应该解析出实体, 而不是原样返回字符串"
    node = parts[0]
    assert isinstance(node, RichTextDateTime)
    assert node.date_time_format == "Dt"
    assert node.date.timestamp() == 1790855311


def test_blocks_path_still_parses_html_links_afterwards():
    """时间戳实体会把后面的链接一起吃掉吗 —— 两者要各自独立解析"""

    from plugins.parse.rich_blocks import parse_inline

    parts = parse_inline(
        '<tg-time unix="1790855311" format="Dt">10月1日</tg-time> · '
        '<a href="https://x.com/a/status/1">来源</a>'
    )
    kinds = [type(n).__name__ for n in parts if not isinstance(n, str)]
    assert "RichTextDateTime" in kinds
    assert "RichTextUrl" in kinds


def test_inline_result_folds_when_a_spoiler_tag_is_given():
    """inline 的**现场解析**结果项也要打码 —— 以前这条路径漏传了标记,

    表现: 缓存命中的结果项有折叠、现场解析的没有 —— 用户选中哪个全看运气。
    """
    from plugins.parse.inline import build_inline_rich_content

    content = build_inline_rich_content(
        make_result(), lang="zh-hans", config=_config(), spoiler_tag="#不可以色色"
    )
    markdown = content.rich_message.markdown
    assert "<details>" in markdown
    assert "⚠️ #不可以色色" in markdown


def test_inline_result_without_a_tag_is_not_folded():
    from plugins.parse.inline import build_inline_rich_content

    content = build_inline_rich_content(make_result(), lang="zh-hans", config=_config())
    assert "<details>" not in content.rich_message.markdown


def test_reply_markup_only_for_media():
    """键盘是用来换 inline_message_id 的, 只有需要二次编辑 (补媒体) 时才挂"""
    assert inline_reply_markup(make_result()) is None
    assert inline_reply_markup(make_result(media=[ImageRef(url="https://a/x.jpg")])) is not None


# ── 选中后立刻摘掉键盘 ─────────────────────────────────────────────────
#
# 键盘是拿 inline_message_id 的**代价**: Telegram 只在消息带 inline keyboard
# 时才回传句柄, 而用户并不需要那个"原链接"按钮。所以约定是"到手就摘"。
#
# 回归: 占位+编辑那次重构把摘键盘的调用**连同注释一起删掉了**, 变成死代码,
# 于是按钮一直留在消息上 (用户报「inline最开始占位消息下面有个链接按钮」)。
# 这里盯住"确实调了", 因为这是纯副作用, 删掉了没有任何测试会红。


def test_the_keyboard_is_dropped_once_the_handle_is_available():
    import types
    from unittest.mock import AsyncMock, patch

    from plugins.parse import inline as inline_mod
    from plugins.parse.inline import inline_result_download

    dropped = AsyncMock()
    pipeline_cls = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    pipeline_cls.return_value.__enter__.return_value.run = AsyncMock(return_value=None)

    chosen = types.SimpleNamespace(
        result_id=inline_mod.RICH_RESULT_ID,
        inline_message_id="BQAAAH8EAACb7f-02gbl1xUz2F4",
        query="https://x.com/a/status/1",
        from_user=types.SimpleNamespace(id=1, language_code="zh-hans"),
    )

    @__import__("contextlib").asynccontextmanager
    async def fake_session():
        yield None

    with (
        patch.object(inline_mod, "get_session", fake_session),
        patch.object(
            inline_mod,
            "UserService",
            return_value=types.SimpleNamespace(ensure_lang=AsyncMock(return_value="zh-hans")),
        ),
        patch.object(
            inline_mod,
            "SettingsService",
            return_value=types.SimpleNamespace(get_config_by_user=AsyncMock(return_value=_config())),
        ),
        patch.object(
            inline_mod,
            "ParseService",
            return_value=types.SimpleNamespace(get_raw_url=AsyncMock(return_value="https://x.com/a/status/1")),
        ),
        patch.object(inline_mod.parse_cache, "get", AsyncMock(return_value=None)),
        patch.object(inline_mod, "_drop_inline_keyboard", dropped),
        patch.object(inline_mod, "ParsePipeline", pipeline_cls),
    ):
        asyncio.run(inline_result_download(types.SimpleNamespace(), chosen))

    dropped.assert_awaited_once()
    assert dropped.await_args.args[1] == chosen.inline_message_id


def test_no_handle_means_no_keyboard_to_drop():
    """没有句柄 = 这条结果本来没挂键盘 (纯文字, 无需二次编辑)"""
    import types
    from unittest.mock import AsyncMock, patch

    from plugins.parse import inline as inline_mod
    from plugins.parse.inline import inline_result_download

    dropped = AsyncMock()
    chosen = types.SimpleNamespace(
        result_id=inline_mod.RICH_RESULT_ID,
        inline_message_id=None,
        query="https://x.com/a/status/1",
        from_user=types.SimpleNamespace(id=1, language_code="zh-hans"),
    )
    with patch.object(inline_mod, "_drop_inline_keyboard", dropped):
        asyncio.run(inline_result_download(types.SimpleNamespace(), chosen))
    dropped.assert_not_awaited()


def test_rich_result_has_id_and_no_external_media_syntax():
    result = make_result(media=[VideoRef(url="https://a/v.mp4", width=8, height=6)])
    results = asyncio.run(build_inline_results(result, None, "zh-hans", _config()))
    assert len(results) == 1
    item = results[0]
    assert item.id == "rich"  # 选中回调靠它识别
    markdown = item.input_message_content.rich_message.markdown
    assert "![" not in markdown
    assert "<footer>" in markdown


def test_rich_result_strips_images_from_note_content():
    """长文正文自带 ![](url) 时也要剥掉, 否则整条 inline 回答被 Telegram 拒"""
    result = RichTextParseResult(title="T", markdown_content="正文\n\n![](https://a/inline.png)")
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/u/status/1"
    results = asyncio.run(build_inline_results(result, None, "zh-hans", _config()))
    markdown = results[0].input_message_content.rich_message.markdown
    assert "![" not in markdown
    assert "正文" in markdown


def test_inline_cover_url_prefers_video_thumb():
    """视频/动图用平台缩略图当封面, 图片用自己的地址"""
    from parsehub.types import AniRef

    assert inline_cover_url(make_result(media=[VideoRef(url="https://a/v.mp4", thumb_url="https://a/c.jpg")])) == "https://a/c.jpg"
    assert inline_cover_url(make_result(media=[AniRef(url="https://a/a.mp4", thumb_url="https://a/ac.jpg")])) == "https://a/ac.jpg"
    assert inline_cover_url(make_result(media=[ImageRef(url="https://a/i.jpg", thumb_url="https://a/t.jpg")])) == "https://a/t.jpg"


def test_inline_cover_url_without_media():
    assert inline_cover_url(make_result()) == ""


def test_media_result_stays_rich_text():
    """有媒体也必须走富文本 Article。

    曾经用 InlineQueryResultPhoto 做封面占位, 结果发出来是一张图 + caption,
    富文本的排版/图集/标签/页脚全丢 —— 用封面图当 thumb_url 就够, 不能换成 Photo 结果。
    """
    result = make_result(media=[VideoRef(url="https://a/v.mp4", thumb_url="https://a/c.jpg")])
    results = asyncio.run(build_inline_results(result, None, "zh-hans", _config()))
    item = results[0]
    assert type(item).__name__ == "InlineQueryResultArticle"
    assert item.thumb_url == "https://a/c.jpg"
    assert item.id == "rich"
    assert isinstance(item.input_message_content, InputRichMessageContent)
    assert item.reply_markup is not None  # 需要句柄才能在选中后替换


def test_single_media_ref_does_not_break_cover_lookup():
    """media 是单个 ref (非 list) 时取封面不能抛"""
    result = make_result(media=VideoRef(url="https://a/v.mp4", thumb_url="https://a/c.jpg"))
    results = asyncio.run(build_inline_results(result, None, "zh-hans", _config()))
    assert results[0].thumb_url == "https://a/c.jpg"


def _config():
    from repo.settings import SettingsConfig

    return SettingsConfig()


# ---------------------------------------------------------------- 解析失败的结果项

def test_build_error_result_puts_the_reason_in_the_sent_message():
    """结果列表里显示原因，**点选后发出去的那条也要带** ——

    用户报：「发出去只有个解析失败，没有详细信息」——
    以前 ``input_message_content`` 只放了标题，description 的 detail 没带过去。
    """
    from i18n import t_
    from plugins.parse.inline import build_error_result

    reason = "解析错误: 无法解析 YouTube 视频: LIVE_STREAM_OFFLINE: Premieres in 23 hours"
    result = build_error_result(t_["zh-hans"], reason)

    assert result.title == "解析失败"
    assert "Premieres in 23 hours" in result.description
    assert "Premieres in 23 hours" in result.input_message_content.message_text


def test_build_error_result_without_a_reason_still_explains_itself():
    from i18n import t_
    from plugins.parse.inline import build_error_result

    result = build_error_result(t_["zh-hans"], "")
    assert "解析失败" in result.input_message_content.message_text
    assert "稍后再试" in result.input_message_content.message_text


def test_the_error_result_never_leaks_more_than_100_chars_of_the_exception():
    """异常里常混着长技术细节 —— 截断到 100 字符，别把整段堆栈甩给用户"""
    from i18n import t_
    from plugins.parse.inline import build_error_result

    result = build_error_result(t_["zh-hans"], "x" * 500)
    assert len(result.description) <= 101, len(result.description)
    assert result.description.endswith("…")
