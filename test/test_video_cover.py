"""视频封面: 三条发送路径都要带上它（inline 记账 / 缓存命中 / 抓不到时的兜底）。

背景（2026-10-06, 用户报「B站视频没有封面」）: 封面早就实现了, 但**只在直发的
"本地 thumb" 那条路上** —— 一旦媒体被换成 file_id（inline 记账、缓存命中）封面就没了,
而用户主要走 inline。另外富文本路径缺 ``WebpageCurlFailed`` 兜底（普通路径一直有）,
封面 URL 服务端抓不到时整条发送会失败。
"""

from types import SimpleNamespace

from pyrogram.types import InputMediaVideo, InputRichMessage, InputRichMessageMedia

from plugins.parse.inline_rich import strip_video_cover


def _rich_with_cover(cover="https://i2.hdslb.com/bfs/archive/x.jpg"):
    item = InputRichMessageMedia(id="m0", media=InputMediaVideo("file_id", video_cover=cover, thumb="/tmp/c.jpg"))
    return InputRichMessage(markdown="正文", media=[item]), item


def test_strip_removes_the_remote_cover_only():
    """兜底只清远端地址 (服务端要去抓的那个), 本地 thumb 保留"""
    rich, item = _rich_with_cover()
    strip_video_cover(rich)
    assert item.media.video_cover is None
    assert item.media.thumb == "/tmp/c.jpg"


def test_strip_reaches_covers_inside_blocks():
    """blocks 路径 (敏感内容/引用卡片) 的封面藏在块里, 同样要能清"""
    video = InputMediaVideo("file_id", video_cover="https://example.com/c.jpg")
    block = SimpleNamespace(video=video, blocks=None)
    rich = SimpleNamespace(media=None, blocks=[block])
    strip_video_cover(rich)
    assert video.video_cover is None


def test_strip_is_safe_on_media_without_a_cover():
    rich, item = _rich_with_cover(cover=None)
    assert strip_video_cover(rich) is rich
    assert item.media.video_cover is None


def test_the_cache_media_builder_uses_the_stored_cover():
    """缓存命中也必须带封面: 缓存里存了 cover_file_id 就要当 thumb 用"""
    from plugins.parse.inline_rich import cache_media_blocks
    from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult

    entry = CacheEntry(
        parse_result=CacheParseResult(title="t", content="c", author_name="", author_handle="", author_url=""),
        media=[CacheMedia(type=CacheMediaType.VIDEO, file_id="vid", cover_file_id="cov")],
        rich=True,
    )
    media, _ph, _blocks, _q, _r, _urls = cache_media_blocks(entry)
    assert media[0].media.thumb == "cov", "缓存里的封面 file_id 应作为 thumb"


def test_the_upload_path_sends_the_cover_along():
    """inline 记账上传 document 时**必须**带 thumb, 否则换成的 file_id 引用没有封面。

    这是纯副作用的一行 (删掉后测试不会红), 用源码断言看着。
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "plugins" / "parse" / "inline_rich.py"
    ).read_text(encoding="utf-8")
    assert "thumb=thumb" in text, "上传 document 时要带封面"
    assert 'thumb_path = getattr(inner, "thumb", None)' in text
    assert "cover_file_id=cover" in text, "记账时要存封面的 file_id"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
