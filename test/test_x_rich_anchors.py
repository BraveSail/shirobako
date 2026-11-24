"""Decorative X punctuation must not split links in Telegram TL."""

import asyncio
from io import BytesIO

import pytest
from parsehub.types import MultimediaParseResult, Platform
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichMessage

from plugins.helpers import build_rich_markdown
from plugins.parse.inline import build_cached_rich_result, build_inline_rich_content
from plugins.parse.inline_rich import rich_cache_entry
from plugins.parse.rich_blocks import markdown_to_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services.cache import CacheEntry

URL = "https://x.com/toaru_project/status/2124820266370900014"


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, TLObject):
        yield value
        for field in type(value).__slots__:
            yield from _nodes(getattr(value, field, None))


@pytest.mark.parametrize("path", ["direct", "inline", "cached_inline"])
def test_x_episode_banner_retains_original_text_and_real_hashtags_in_tl(path):
    result = MultimediaParseResult(
        content="番組 #アニメ\n\n||◤ #08「作戦」◢||\n\n||◤ #少女共棲 ◢||",
        hashtags=["アニメ", "少女共棲"],
        author_name="とあるプロジェクト公式", author_url="https://x.com/toaru_project",
    )
    result.raw_url = URL
    result.platform = Platform.TWITTER
    config = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_tags": True})
    if path == "cached_inline":
        entry = CacheEntry.model_validate_json(rich_cache_entry(result, []).model_dump_json())
        message = build_cached_rich_result(entry, URL, "zh-hans", config).input_message_content.rich_message
    elif path == "inline":
        message = build_inline_rich_content(result, lang="zh-hans", config=config).rich_message
    else:
        markdown = build_rich_markdown(result, lang="zh-hans", config=config)
        message = InputRichMessage(blocks=markdown_to_blocks(markdown))

    async def encode():
        return await message.write(client=None)

    raw = asyncio.run(encode())
    blocks = TLObject.read(BytesIO(raw.write())).blocks
    nodes = list(_nodes(blocks))
    plain = "".join(node.text for node in nodes if isinstance(node, tl.TextPlain))
    assert "<a " not in plain and "</a>" not in plain
    assert "||◤ #08「作戦」◢||" in plain
    assert "||◤ #少女共棲 ◢||" in plain
    assert not any(isinstance(node, tl.TextSpoiler) for node in nodes)
    links = [node for node in nodes if isinstance(node, tl.TextUrl)]
    assert [node.text.text for node in links if "/hashtag/" in node.url].count("#少女共棲") == 1
    assert {node.text.text for node in links if "/hashtag/" in node.url} == {"#アニメ", "#少女共棲"}
    assert isinstance(blocks[-1], tl.PageBlockFooter)
    assert any(isinstance(node, tl.TextUrl) and node.url == URL for node in _nodes(blocks[-1]))