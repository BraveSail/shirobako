"""x.com/article/<id> 长文解析：URL 识别、媒体合并、封面补齐。

article id 与 status id 是**同一个 id 空间**（实测直接查 TweetDetail 就返回那条
推文，正文在 ``article_results`` 里），所以只是 URL 形态不同，取数路径不变。

三个坑：
1. ``__match__`` / ``get_id_by_url`` 只认 ``/status/`` ⇒ article 链接根本不被识别。
2. article 的媒体在 ``tweet.article.media``，不在 ``tweet.media`` ⇒ 不合并就是零图。
3. article 正文开头有 ``![Cover Image](cover_url)``，但封面来自 ``cover_media``、
   不在 ``media_entities`` 里 ⇒ 那张图对不上 media，渲染时被剥掉。
"""

from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.provider_api.twitter import ArticleRenderer, Twitter

ARTICLE_URL = "https://x.com/TheFluke999/article/2053655686765314432"


def test_article_url_is_matched():
    assert TwitterParser.match(ARTICLE_URL)
    assert TwitterParser.match("https://x.com/someone/article/123")
    # status 链接照旧
    assert TwitterParser.match("https://x.com/someone/status/123")


def test_get_id_by_url_accepts_article_path():
    assert Twitter.get_id_by_url(ARTICLE_URL) == "2053655686765314432"
    assert Twitter.get_id_by_url("https://x.com/a/status/999") == "999"
    # 都不是时仍然报错
    try:
        Twitter.get_id_by_url("https://x.com/a/profile")
    except ValueError:
        pass
    else:
        raise AssertionError("非推文/文章链接应当报错")


def test_article_result_requires_media_download():
    """长文结果必须声明 requires_media_download=True。

    长文的图被抽成附件放在 ``media`` 里、正文只留外链 markdown。不下载的话流水线
    走「无媒体」分支（``processed_list`` 为空），渲染层拿不到 ``media_urls`` 就把
    正文里的图**全部剥掉** —— 用户看到「没图」（实测 6 张全丢）。
    判据要落在 **result 类**上，不是 parser 类（那个属性不会被读到）。
    """
    from parsehub.parsers.parser.twitter import TwitterArticleParseResult
    from parsehub.types import RichTextParseResult

    assert TwitterArticleParseResult.requires_media_download is True
    # 普通长文/普通推文仍保持 False：它们的正文图是外链，Telegram 自己抓
    assert RichTextParseResult.requires_media_download is False
    assert issubclass(TwitterArticleParseResult, RichTextParseResult)


def _article_data(*, cover: str = "", entities: list[dict] | None = None) -> dict:
    return {
        "title": "标题",
        "content_state": {"blocks": [{"key": "1", "text": "正文", "type": "unstyled"}]},
        "entityMap": [],
        "media_entities": entities or [],
        "cover_media": {"media_info": {"original_img_url": cover}} if cover else {},
    }


def test_article_cover_is_added_to_media():
    """封面写进了正文，就必须也在媒体列表里 —— 否则渲染时那张图被剥掉。"""
    article = ArticleRenderer(_article_data(cover="https://pbs.twimg.com/cover.jpg")).render()
    assert article.content.startswith("![Cover Image](https://pbs.twimg.com/cover.jpg)")
    urls = [m.url for m in (article.media or [])]
    assert urls == ["https://pbs.twimg.com/cover.jpg"], "封面必须在媒体列表里（否则正文图丢失）"


def test_article_cover_comes_first_when_entities_exist():
    """封面排在媒体列表最前（正文里它就是第一个）。"""
    entities = [
        {"media_id": 1, "media_info": {"__typename": "ApiImage", "original_img_url": "https://pbs.twimg.com/a.jpg"}},
    ]
    article = ArticleRenderer(
        _article_data(cover="https://pbs.twimg.com/cover.jpg", entities=entities)
    ).render()
    urls = [m.url for m in (article.media or [])]
    assert urls[0] == "https://pbs.twimg.com/cover.jpg"
    assert "https://pbs.twimg.com/a.jpg" in urls


def test_article_without_cover_has_no_extra_media():
    entities = [
        {"media_id": 1, "media_info": {"__typename": "ApiImage", "original_img_url": "https://pbs.twimg.com/a.jpg"}},
    ]
    article = ArticleRenderer(_article_data(entities=entities)).render()
    assert "Cover Image" not in article.content
    assert [m.url for m in (article.media or [])] == ["https://pbs.twimg.com/a.jpg"]
