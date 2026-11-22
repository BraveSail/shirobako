"""Lists survive HTML conversion, rich formatting and Telegram serialization."""

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from parsehub.provider_api.linuxdo import LinuxDoTopic
from parsehub.types import Platform
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichMessage

from plugins.helpers import build_rich_markdown
from plugins.parse.inline import build_cached_rich_result, build_inline_rich_content
from plugins.parse.rich_blocks import markdown_to_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services.cache import CacheEntry, CacheParseResult

URL = "https://linux.do/t/topic/1"
ITEMS = ("第一项", "第二项", "第三项", "第四项", "第五项")


def _result(markdown: str, *, content: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        title="测试帖子", raw_url=URL, content=content, markdown_content=markdown, platform=Platform.LINUXDO,
    )


def _wire_blocks(message):
    async def encode():
        return await message.write(client=None)

    return TLObject.read(BytesIO(asyncio.run(encode()).write())).blocks


def _inline_message(result, *, cached: bool = False):
    config = DEFAULT_SETTINGS_CONFIG
    if cached:
        entry = CacheEntry(
            rich=True,
            parse_result=CacheParseResult(
                title=result.title, platform=Platform.LINUXDO.id, content=result.content,
                markdown_content=result.markdown_content,
            ),
        )
        article = build_cached_rich_result(entry, URL, lang="zh-hans", config=config)
        return article.input_message_content.rich_message
    return build_inline_rich_content(result, lang="zh-hans", config=config).rich_message


@pytest.mark.parametrize("cached", [False, True], ids=["fresh", "cache-hit"])
def test_linuxdo_bullets_are_one_native_list(cached):
    # Same five-item HTML structure as the reported post, with generic text.
    cooked = "<ul>" + "".join(f"<li>{item}</li>" for item in ITEMS) + "</ul>"
    result = _result(LinuxDoTopic._to_markdown(cooked), content="\n".join(ITEMS))
    blocks = _wire_blocks(_inline_message(result, cached=cached))
    lists = [block for block in blocks if isinstance(block, tl.PageBlockList)]
    assert len(lists) == 1
    assert tuple(item.blocks[0].text.text for item in lists[0].items) == ITEMS


def test_ordered_and_unordered_lists_keep_numbering_and_nesting():
    cooked = "<ul><li>甲<ul><li>子项</li></ul></li><li>乙</li></ul><ol start='3'><li>丙</li><li>丁</li></ol>"
    blocks = _wire_blocks(_inline_message(_result(LinuxDoTopic._to_markdown(cooked))))
    bullets = next((b for b in blocks if isinstance(b, tl.PageBlockList)), None)
    numbered = next((b for b in blocks if isinstance(b, tl.PageBlockOrderedList)), None)
    assert bullets is not None and numbered is not None
    assert [item.blocks[0].text.text for item in bullets.items] == ["甲", "乙"]
    sublist = bullets.items[0].blocks[1]
    assert isinstance(sublist, tl.PageBlockList)
    assert sublist.items[0].blocks[0].text.text == "子项"
    assert [(item.value, item.blocks[0].text.text) for item in numbered.items] == [(3, "丙"), (4, "丁")]


def test_loose_list_retains_continuation_paragraph_with_its_item():
    cooked = "<ul><li><p>第一项</p><p><strong>续段</strong></p></li><li>第二项</li></ul><p>正文</p>"
    blocks = _wire_blocks(_inline_message(_result(LinuxDoTopic._to_markdown(cooked))))
    bullets = next((b for b in blocks if isinstance(b, tl.PageBlockList)), None)
    assert bullets is not None and len(bullets.items) == 2
    first, second = bullets.items
    assert first.blocks[0].text.text == "第一项"
    rendered = first.blocks[1].text
    fragments = rendered.texts if isinstance(rendered, tl.TextConcat) else [rendered]
    assert [part.text.text for part in fragments if isinstance(part, tl.TextBold)] == ["续段"]
    assert second.blocks[0].text.text == "第二项"
    assert any(isinstance(b, tl.PageBlockParagraph) and getattr(b.text, "text", None) == "正文" for b in blocks)


@pytest.mark.parametrize("hide_content", ["", "#剧透"], ids=["normal", "spoiler"])
def test_list_item_starting_with_number_keeps_its_text_at_the_same_level(hide_content):
    cooked = "<ul><li>3. 只是文字<ul><li>子项</li></ul></li></ul>"
    message = build_inline_rich_content(
        _result(LinuxDoTopic._to_markdown(cooked)), lang="zh-hans",
        config=DEFAULT_SETTINGS_CONFIG, spoiler_tag=hide_content,
    ).rich_message
    blocks = _wire_blocks(message)
    container = next((b.blocks for b in blocks if isinstance(b, tl.PageBlockDetails)), blocks)
    bullets = next(b for b in container if isinstance(b, tl.PageBlockList))
    text, nested = bullets.items[0].blocks
    assert isinstance(text, tl.PageBlockParagraph) and text.text.text == "3. 只是文字"
    assert isinstance(nested, tl.PageBlockList) and nested.items[0].blocks[0].text.text == "子项"


def test_list_structure_is_preserved_inside_an_automatically_folded_body():
    result = _result("前文" * 350 + "\n\n- [x] 已完成\n- [ ] 未完成")
    markdown = build_rich_markdown(result, config=DEFAULT_SETTINGS_CONFIG, lang="zh-hans")
    blocks = _wire_blocks(InputRichMessage(blocks=markdown_to_blocks(markdown)))
    details = next((b for b in blocks if isinstance(b, tl.PageBlockDetails)), None)
    assert details is not None
    bullets = next((b for b in details.blocks if isinstance(b, tl.PageBlockList)), None)
    assert bullets is not None
    assert [(item.checkbox, bool(item.checked)) for item in bullets.items] == [(True, True), (True, False)]


@pytest.mark.parametrize("indent", ["", "  "], ids=["flush", "indented"])
def test_preview_does_not_split_a_list_in_the_middle(indent):
    # The seven-line preview would take the first two items of this list.
    source = "\n".join(["前文"] * 5 + ["", *(f"{indent}* {item}" for item in ITEMS), "", "后文"])
    blocks = _wire_blocks(_inline_message(_result(source)))
    assert not any(isinstance(b, tl.PageBlockList) for b in blocks)
    details = next(b for b in blocks if isinstance(b, tl.PageBlockDetails))
    bullets = next(b for b in details.blocks if isinstance(b, tl.PageBlockList))
    assert tuple(item.blocks[0].text.text for item in bullets.items) == ITEMS


@pytest.mark.parametrize("tail", ["", "\n\n后文"], ids=["list-only", "followed-by-prose"])
def test_a_single_long_list_item_is_folded_intact(tail):
    text = "长列表项" * 180
    blocks = _wire_blocks(_inline_message(_result(f"* {text}{tail}")))
    assert not any(isinstance(b, tl.PageBlockList) for b in blocks)
    details = next((b for b in blocks if isinstance(b, tl.PageBlockDetails)), None)
    assert details is not None
    bullets = next(b for b in details.blocks if isinstance(b, tl.PageBlockList))
    assert [item.blocks[0].text.text for item in bullets.items] == [text]
    if tail:
        assert details.blocks[-1].text.text == tail.strip()


def test_text_after_details_inside_a_list_is_not_lost():
    source = "* <details><summary>展开</summary>\n  折叠内文\n  </details>尾文\n* 第二项"
    (bullets,) = _wire_blocks(InputRichMessage(blocks=markdown_to_blocks(source)))
    details, tail = bullets.items[0].blocks
    assert isinstance(details, tl.PageBlockDetails) and details.blocks[0].text.text == "折叠内文"
    assert isinstance(tail, tl.PageBlockParagraph) and tail.text.text == "尾文"


def test_plain_text_asterisks_and_dots_do_not_turn_into_list_markers():
    source = "* 字面星号\n3. 字面编号"
    blocks = _wire_blocks(_inline_message(_result("", content=source)))
    assert not any(isinstance(b, (tl.PageBlockList, tl.PageBlockOrderedList)) for b in blocks)
    assert any(
        isinstance(b, tl.PageBlockParagraph) and getattr(b.text, "text", None) == source.replace("\n", "  \n")
        for b in blocks
    )


def test_escaped_list_like_text_and_fenced_code_stay_literal():
    source = "\\* 字面星号\n\n```text\n* 代码\n3. 代码\n```"
    blocks = _wire_blocks(_inline_message(_result(source)))
    assert not any(isinstance(b, (tl.PageBlockList, tl.PageBlockOrderedList)) for b in blocks)
    code = next(b for b in blocks if isinstance(b, tl.PageBlockPreformatted)).text.text
    assert [line.rstrip() for line in code.splitlines()] == ["* 代码", "3. 代码"]
