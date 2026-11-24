"""X Status 的内部缓存必须无损还原实体，并保持公开输出不变。"""

import json

import pytest

from parsehub.parsers.parser.twitter import TwitterArticleParseResult
from parsehub.types.media_ref import ImageRef
from parsehub.types.platform import Platform
from parsehub.types.result import MultimediaParseResult, Poll, PollOption
from parsehub.types.serialize import result_from_cache_dict, result_to_cache_dict
from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending


@pytest.fixture
def x_status_result() -> MultimediaParseResult:
    result = MultimediaParseResult(
        content="旧版正文 {{poll:0}}",
        media=[ImageRef(url="https://cdn.example/trending.jpg"), ImageRef(url="https://cdn.example/quote.jpg")],
        quoted_media_count=1,
        polls=[Poll(options=[PollOption(text="选项", count=2)], total_votes=2)],
        x_status=XStatus(
            main=XPost(
                segments=(
                    TextRun("😀  **字面**\n"),
                    TextRun("#話題", "https://x.com/hashtag/話題", ("bold", "italic")),
                ),
                author_name="主帖作者",
                author_handle="main",
                author_url="https://x.com/main",
                poll_index=0,
                youtube=TextRun("影片", "https://youtu.be/video"),
            ),
            reply=XPost(segments=(TextRun("回复"),), author_name="回复作者"),
            quoted=XPost(segments=(), author_name="仅有配图的引用"),
            trending=XTrending("趋势", "摘要", "https://x.com/i/trending/1", cover_index=0),
        ),
    )
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/main/status/1"
    return result


def test_x_status_dto_survives_json_roundtrip(x_status_result):
    payload = json.loads(json.dumps(result_to_cache_dict(x_status_result)))
    assert isinstance(payload["x_status"]["main"]["segments"], list)
    rebuilt = result_from_cache_dict(payload)
    status = rebuilt.x_status

    assert isinstance(status, XStatus)
    assert isinstance(status.main, XPost)
    assert isinstance(status.main.segments, tuple)
    assert isinstance(status.main.segments[1], TextRun)
    assert isinstance(status.main.segments[1].styles, tuple)
    assert isinstance(status.main.youtube, TextRun)
    assert isinstance(status.reply, XPost)
    assert isinstance(status.quoted, XPost)
    assert isinstance(status.trending, XTrending)
    assert status == x_status_result.x_status
    assert rebuilt.to_dict() == x_status_result.to_dict()


def test_x_status_dto_is_missing_from_public_dict(x_status_result):
    assert "x_status" not in x_status_result.to_dict()


def test_x_status_payload_without_optional_fields_uses_dto_defaults():
    result = MultimediaParseResult(content="原有正文")
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/main/status/1"
    payload = result_to_cache_dict(result)
    payload["x_status"] = {"main": {"segments": [{"text": "字面量"}]}}
    rebuilt = result_from_cache_dict(json.loads(json.dumps(payload)))
    assert rebuilt.x_status == XStatus(main=XPost(segments=(TextRun("字面量"),)))


def test_old_non_x_cache_still_loads_without_x_status_or_content_version():
    result = MultimediaParseResult(content="Threads 正文")
    result.platform = Platform.THREADS
    result.raw_url = "https://www.threads.com/@example/post/1"
    payload = json.loads(json.dumps(result_to_cache_dict(result)))
    assert "x_status" not in payload
    del payload["content_version"]

    rebuilt = result_from_cache_dict(payload)
    assert rebuilt.x_status is None
    assert rebuilt.to_dict() == result.to_dict()


def test_x_article_still_loads_without_x_status():
    article = TwitterArticleParseResult(markdown_content="## X Article\n\n原有正文")
    article.platform = Platform.TWITTER
    article.raw_url = "https://x.com/main/article/1"
    payload = json.loads(json.dumps(result_to_cache_dict(article)))
    assert "x_status" not in payload

    rebuilt = result_from_cache_dict(payload)
    assert type(rebuilt) is TwitterArticleParseResult
    assert rebuilt.x_status is None
    assert rebuilt.markdown_content == article.markdown_content
