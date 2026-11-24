from datetime import UTC

# ---------------------------------------------------------------- 页脚 i18n


def _footer_of(markdown: str) -> str:
    return markdown.split("<footer>")[-1].replace("</footer>", "").strip()


def test_source_label_follows_the_user_language():
    """来源标签跟随用户语言 —— 以前硬编码英文 "Source（…）", 任何语言都显示英文"""
    from datetime import datetime
    from types import SimpleNamespace

    from plugins.helpers import build_rich_markdown

    result = SimpleNamespace(
        title="", content="正文", raw_url="https://x.com/u/status/1",
        author_name="", author_handle="", author_url="",
        published_at=datetime(2026, 10, 4, 1, 37, tzinfo=UTC),
        view_count=10, like_count=2, tags=None, platform=None, media=None, markdown_content="正文",
    )
    config = SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)

    assert "来源（Twitter）" in _footer_of(build_rich_markdown(result, config=config, lang="zh-hans"))
    assert "來源（Twitter）" in _footer_of(build_rich_markdown(result, config=config, lang="zh-hant"))
    assert "Source (Twitter)" in _footer_of(build_rich_markdown(result, config=config, lang="en-us"))
    assert "ソース（Twitter）" in _footer_of(build_rich_markdown(result, config=config, lang="ja-jp"))


def test_views_and_likes_follow_the_user_language():
    """查看/点赞都跟随语言 —— 点赞以前走模块级 t_ (默认语言), 与查看语言不一致"""
    from datetime import datetime

    from plugins.helpers import build_metadata_line

    at = datetime(2026, 10, 4, 1, 37, tzinfo=UTC)
    # 时间那段是客户端渲染的 tg-time 实体 (见 test_author_cache 的 _time_tag), 这里只比对
    # 它之后的「查看/点赞」—— 本测试要盯的就是这两个标签跟随语言
    assert build_metadata_line(published_at=at, view_count=10, like_count=2, lang="zh-hans").endswith(
        " · 10 查看 · 2 点赞"
    )
    assert build_metadata_line(published_at=at, view_count=10, like_count=2, lang="en-us").endswith(
        " · 10 views · 2 likes"
    )
    assert build_metadata_line(published_at=at, view_count=10, like_count=2, lang="ja-jp").endswith(
        " · 10 表示 · 2 いいね"
    )


def test_date_format_uses_kanji_for_chinese_and_japanese():
    """中日都用年月日书写; 其余语言用 ISO 日期"""
    from datetime import datetime

    from plugins.helpers import build_metadata_line

    at = datetime(2026, 10, 4, 1, 37, tzinfo=UTC)
    assert "2026年10月4日" in build_metadata_line(published_at=at, lang="zh-hans")
    assert "2026年10月4日" in build_metadata_line(published_at=at, lang="ja-jp")
    assert "2026-10-04" in build_metadata_line(published_at=at, lang="en-us")
    assert "2026-10-04" in build_metadata_line(published_at=at, lang="ru-ru")
