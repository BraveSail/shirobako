"""Keep the Instagram profile link in both inline delivery paths."""

import asyncio
from unittest.mock import AsyncMock, patch

from parsehub import ParseHub
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.provider_api.instagram import InstagramPost
from pyrogram.raw import types as tl

from plugins.parse.inline import build_cached_rich_result, build_inline_rich_content
from plugins.parse.inline_rich import rich_cache_entry
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services.cache import CacheEntry

POST_URL = "https://www.instagram.com/p/Example/"
AUTHOR_URL = "https://www.instagram.com/actual_author/"


def _parts(text):
    yield text
    if isinstance(text, tl.TextConcat):
        for part in text.texts:
            yield from _parts(part)
    elif isinstance(text, (tl.TextBold, tl.TextUrl, tl.TextFixed)):
        yield from _parts(text.text)


def test_instagram_author_name_links_to_profile_in_fresh_and_cached_inline():
    post = InstagramPost({
        "__typename": "GraphImage",
        "display_url": "https://example.org/photo.jpg",
        "owner": {"full_name": "作者 / Pen name", "username": "actual_author"},
    })

    async def verify():
        with patch.object(InstagramParser, "_parse", new=AsyncMock(return_value=post)):
            result = await ParseHub().parse(POST_URL)
        config = DEFAULT_SETTINGS_CONFIG
        entry = CacheEntry.model_validate_json(rich_cache_entry(result, []).model_dump_json())
        fresh = build_inline_rich_content(result, lang="zh-hans", config=config)
        cached = build_cached_rich_result(entry, POST_URL, "zh-hans", config).input_message_content
        for content in (fresh, cached):
            raw = await content.rich_message.write(client=None)
            paragraphs = (block for block in raw.blocks if isinstance(block, tl.PageBlockParagraph))
            parts = [part for paragraph in paragraphs for part in _parts(paragraph.text)]
            links = [part for part in parts if isinstance(part, tl.TextUrl) and part.url == AUTHOR_URL]
            assert len(links) == 1
            assert any(isinstance(part, tl.TextPlain) and part.text == "作者 / Pen name" for part in _parts(links[0].text))
            assert any(isinstance(part, tl.TextFixed) and part.text.text == "@actual_author" for part in parts)

    asyncio.run(verify())
