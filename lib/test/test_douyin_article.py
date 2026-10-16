"""抖音长图文（/article/<id>）解析的离线测试。

被测点:
1. ``get_aweme_id`` 认 ``/article/<id>``
2. ``DouyinApiResult.parse`` 对 ``article_info`` 走 ARTICLE 分支（不落进 images/video）
3. ``article_content`` 双重编码的解析 + 解不出时退回 abstract
4. 标题取 ``article_title``；元数据与视频共用同一批字段
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parsehub.errors import ParseError  # noqa: E402
from parsehub.parsers.parser.douyin import (  # noqa: E402
    DouyinApiResult,
    DouyinMediaType,
)
from parsehub.provider_api.douyin import DouyinMobileCrawler  # noqa: E402

ARTICLE_ID = "7696571479932144911"


def _article_payload(
    *,
    markdown: str = "# 正文标题\n\n这是**文章**正文。",
    abstract: str = "摘要",
    title: str = "文章标题",
    content_raw: str | None = None,
) -> dict:
    """构造一条 ``aweme_type=163`` 的响应（字段形态取自真实响应）。"""
    content = content_raw
    if content is None:
        content = json.dumps({"long_article_abstract": abstract, "markdown": markdown}, ensure_ascii=False)
    return {
        "aweme_detail": {
            "aweme_id": ARTICLE_ID,
            "aweme_type": 163,
            "desc": "短摘要句 #tag",
            "create_time": 1791997703,
            "article_info": {
                "article_title": title,
                "article_content": content,
                "article_id": ARTICLE_ID,
                "read_time": 15,
            },
            "author": {"nickname": "xm", "sec_uid": "MS4wLjABAAAAtest", "uid": "12345"},
            "statistics": {"digg_count": 7431, "comment_count": 4808, "play_count": 0},
        }
    }


class TestGetAwemeId(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_article_url(self):
        """``/article/<id>`` 要能取出 id（用户报的那条链接的形态）。"""
        got = await DouyinMobileCrawler.get_aweme_id(f"https://www.douyin.com/article/{ARTICLE_ID}")
        self.assertEqual(got, ARTICLE_ID)

    async def test_still_accepts_video_urls(self):
        """既有形态不能回归。"""
        self.assertEqual(await DouyinMobileCrawler.get_aweme_id("https://www.douyin.com/video/123456"), "123456")
        self.assertEqual(await DouyinMobileCrawler.get_aweme_id("https://v.douyin.com/share/video/789"), "789")
        self.assertEqual(await DouyinMobileCrawler.get_aweme_id("https://www.douyin.com/?modal_id=42"), "42")

    async def test_rejects_unknown_url(self):
        with self.assertRaises(ParseError):
            await DouyinMobileCrawler.get_aweme_id("https://www.douyin.com/user/abc")


class TestArticleParse(unittest.TestCase):
    def test_uses_markdown_body(self):
        result = DouyinApiResult.parse(_article_payload())
        self.assertIs(result.type, DouyinMediaType.ARTICLE)
        self.assertEqual(result.article_markdown, "# 正文标题\n\n这是**文章**正文。")
        self.assertEqual(result.article_title, "文章标题")
        # 元数据与视频共用同一批字段
        self.assertEqual(result.author_name, "xm")
        self.assertEqual(result.author_sec_uid, "MS4wLjABAAAAtest")
        self.assertEqual(result.like_count, 7431)
        self.assertIsNone(result.view_count)  # play_count=0 → 视为没有

    def test_falls_back_to_abstract_when_markdown_missing(self):
        """markdown 缺、abstract 有 → 用 abstract（不报错）。"""
        payload = _article_payload(content_raw=json.dumps({"long_article_abstract": "只有摘要"}))
        result = DouyinApiResult.parse(payload)
        self.assertIs(result.type, DouyinMediaType.ARTICLE)
        self.assertEqual(result.article_markdown, "只有摘要")

    def test_raises_when_content_undecodable(self):
        """``article_content`` 不是 JSON 且拿不到 abstract → 明确报错（不静默出空）。"""
        payload = _article_payload(content_raw="not json at all")
        with self.assertRaisesRegex(ParseError, "article_content 里没有正文"):
            DouyinApiResult.parse(payload)

    def test_raises_when_both_empty(self):
        payload = _article_payload(content_raw=json.dumps({}))
        with self.assertRaisesRegex(ParseError, "article_content 里没有正文"):
            DouyinApiResult.parse(payload)

    def test_article_wins_over_images(self):
        """响应里同时有 images 时也必须走 ARTICLE（真实响应里 image_list 常非空）。"""
        payload = _article_payload()
        payload["aweme_detail"]["images"] = [
            {"url_list": ["https://example.com/1.jpg"], "width": 100, "height": 100}
        ]
        result = DouyinApiResult.parse(payload)
        self.assertIs(result.type, DouyinMediaType.ARTICLE)
        self.assertTrue(result.article_markdown)

    def test_video_payload_still_parses_as_video(self):
        """没有 article_info 的响应仍走原分支（不回归）。"""
        payload = {
            "aweme_detail": {
                "aweme_id": "1",
                "desc": "视频",
                "video": {
                    "play_addr": {"url_list": ["https://example.com/v.mp4"], "width": 1080, "height": 1920},
                    "duration": 1000,
                    "cover": {"url_list": ["https://example.com/c.jpg"]},
                    "bit_rate": [
                        {
                            "bit_rate": 1000,
                            "play_addr": {"url_list": ["https://example.com/v.mp4"], "width": 1080, "height": 1920},
                        }
                    ],
                },
                "author": {"nickname": "a"},
                "statistics": {"digg_count": 1},
            }
        }
        result = DouyinApiResult.parse(payload)
        self.assertIs(result.type, DouyinMediaType.VIDEO)

    def test_image_payload_still_parses_as_image(self):
        """没有 article_info 的图集响应仍走原分支。"""
        payload = {
            "aweme_detail": {
                "aweme_id": "2",
                "desc": "图集",
                "images": [
                    {"url_list": ["https://example.com/1.jpg"], "width": 100, "height": 200},
                    {"url_list": ["https://example.com/2.jpg"], "width": 100, "height": 200},
                ],
                "author": {"nickname": "a"},
                "statistics": {"digg_count": 2},
            }
        }
        result = DouyinApiResult.parse(payload)
        self.assertIs(result.type, DouyinMediaType.IMAGE)


if __name__ == "__main__":
    unittest.main()
