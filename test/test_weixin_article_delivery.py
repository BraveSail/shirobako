"""The Weixin article survives the download gate and native delivery/cache path."""

import asyncio
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsehub import ParseHub
from parsehub.types import DownloadResult, ImageFile
from parsehub.utils import http
from PIL import Image
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichBlockParagraph, InputRichBlockSectionHeading, InputRichMessage

from plugins.parse import inline_rich
from plugins.parse.rich_blocks import SpoilerPhotoBlock
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services import cache
from services.pipeline import ParsePipeline

URL = "https://mp.weixin.qq.com/s/example-article"
PAGE = Path(__file__).parents[1] / "lib" / "test" / "fixtures" / "weixin_article_sections.html"
CONFIG = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_source": False, "hide_desc": False})


def test_weixin_pipeline_downloads_and_delivers_photo_between_native_paragraphs(monkeypatch, tmp_path):
    monkeypatch.setattr(http.AsyncClient, "get", AsyncMock(return_value=SimpleNamespace(text=PAGE.read_text())))
    img = tmp_path / "source.png"
    Image.new("RGB", (32, 24)).save(img)
    reporter = SimpleNamespace(
        report=AsyncMock(), report_result=AsyncMock(), report_error=AsyncMock(),
        report_progress=AsyncMock(), dismiss=AsyncMock(),
    )
    file_id = FileId(
        file_type=FileType.PHOTO, dc_id=2, media_id=101, access_hash=42, volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL, thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
    ).encode()

    async def exercise():
        result = await ParseHub().parse(URL)
        downloader = AsyncMock(return_value=DownloadResult([ImageFile(path=str(img), width=32, height=24)], tmp_path))
        monkeypatch.setattr(type(result), "download", downloader)
        pipe = ParsePipeline(URL, URL, reporter, parse_result=result, singleflight=False, t=lambda s: s)
        run = await pipe.run()
        downloader.assert_awaited_once()
        assert run is not None and len(run.processed_list) == 1
        media, main, mapped, _, _, urls = inline_rich.build_rich_media(result.media, run.processed_list)
        assert len(media) == 1 and Path(media[0].media.media).is_file()
        media[0].media.media = file_id  # Replace only the Telegram upload boundary.
        cold = inline_rich.build_delivery_blocks(
            result, config=CONFIG, raw_url=URL, lang="zh-hans", media_placeholders=main,
            media_blocks=mapped, media_urls=urls,
        )
        photo_indices = [i for i, block in enumerate(cold) if isinstance(block, SpoilerPhotoBlock)]
        assert len(photo_indices) == 1
        photo_at = photo_indices[0]
        assert isinstance(cold[photo_at - 2], InputRichBlockSectionHeading)
        assert all(isinstance(cold[i], InputRichBlockParagraph) for i in (photo_at - 1, photo_at + 1))
        info = cache.CacheMedia(type=cache.CacheMediaType.PHOTO, file_id=file_id)
        entry = inline_rich.rich_cache_entry(result, [info], urls=urls)
        assert entry is not None
        restored = cache.CacheEntry.model_validate_json(entry.model_dump_json())
        warm, _, _ = inline_rich.build_cached_rich_content(restored, URL, lang="zh-hans", config=CONFIG)
        raw_cold = await InputRichMessage(blocks=cold).write(client=None)
        raw_warm = await InputRichMessage(blocks=warm).write(client=None)
        assert raw_cold.write() == raw_warm.write()
        assert TLObject.read(BytesIO(raw_cold.write())).write() == raw_cold.write()

    asyncio.run(exercise())


def test_weixin_v15_persistent_cache_refreshes_without_refreshing_other_platforms(monkeypatch):
    payload = cache.CacheEntry(parse_result=cache.CacheParseResult(
        platform="weixin", content_version=15, meta_fields=cache.FOOTER_META_FIELDS_VERSION,
        author_name="Publisher", like_count=None, tags=[], markdown_content="article",
        quoted_media_count=0, reply_media_count=0,
    )).model_dump(mode="json")
    load = AsyncMock(return_value=json.dumps(payload))
    monkeypatch.setattr(cache, "get_optional", load)
    reader = cache.PersistentCache()
    assert asyncio.run(reader.get(URL)) is None
    payload["parse_result"]["platform"] = "zhihu"
    load.return_value = json.dumps(payload)
    assert asyncio.run(reader.get("https://www.zhihu.com/question/123/answer/456")) is not None
