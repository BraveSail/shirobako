"""结果对象的缓存往返: 必须**无损**, 尤其是媒体类型与派生字段。

为什么要专门测: 缓存命中时对象是从 JSON 重建的, 一旦重建出的对象与原来行为不同,
表现是"第二次发同一条链接效果变了"(媒体类型错、正文丢失), 而这**不会报错**。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from parsehub.types.media_ref import AniRef, ImageRef, LivePhotoRef, VideoRef
from parsehub.types.platform import Platform
from parsehub.types.result import (
    ImageParseResult,
    MultimediaParseResult,
    RichTextParseResult,
    VideoParseResult,
)
from parsehub.types.serialize import (
    ResultRebuildUnavailable,
    result_from_cache_dict,
    result_to_cache_dict,
)

PUBLISHED = datetime(2026, 10, 1, 19, 48, tzinfo=timezone(timedelta(hours=8)))


class _NeedsRuntimeState(VideoParseResult):
    """测试专用：构造必须传入**解析现场才有的句柄**（``dl``），否则 ``TypeError``。

    用它钉住「构造不了的结果类必须拒绝重建」这条机制，不再依赖生产类
    （原来借用的是 yt-dlp 系的 ``YtVideoParseResult``，那套已随 yt-dlp 移除）。
    """

    def __init__(self, *, dl, title: str = "", content: str = "", **kwargs):
        self.dl = dl
        super().__init__(title=title, content=content, **kwargs)


def _roundtrip(result):
    """过一次 JSON (缓存里存的就是字符串) —— 不经过 JSON 就测不出真实往返。"""
    return result_from_cache_dict(json.loads(json.dumps(result_to_cache_dict(result))))


class RoundtripTest(unittest.TestCase):
    def test_video_result_keeps_every_field(self):
        result = VideoParseResult(
            title="标题",
            video=VideoRef(url="https://cdn.example/v.mp4", thumb_url="https://cdn.example/t.jpg", duration=93),
            content="正文内容",
            author_name="作者",
            author_handle="handle",
            author_url="https://x.com/handle",
            is_sensitive=True,
            published_at=PUBLISHED,
            view_count=138460,
            like_count=21380,
            tags=["AI画像", "足裏"],
            quoted_media_count=1,
            reply_media_count=2,
        )
        result.raw_url = "https://x.com/handle/status/1"
        result.platform = Platform.TWITTER

        back = _roundtrip(result)

        self.assertIsInstance(back, VideoParseResult)
        self.assertEqual(back.title, result.title)
        self.assertEqual(back.content, result.content)
        self.assertEqual(back.author_name, result.author_name)
        self.assertEqual(back.author_handle, result.author_handle)
        self.assertEqual(back.author_url, result.author_url)
        self.assertIs(back.is_sensitive, True)
        self.assertEqual(back.published_at, PUBLISHED)
        self.assertEqual(back.view_count, 138460)
        self.assertEqual(back.like_count, 21380)
        self.assertEqual(back.tags, ["AI画像", "足裏"])
        self.assertEqual(back.quoted_media_count, 1)
        self.assertEqual(back.reply_media_count, 2)
        # raw_url 不在构造参数里 —— 忘了赋回就会丢
        self.assertEqual(back.raw_url, "https://x.com/handle/status/1")
        self.assertEqual(back.platform, Platform.TWITTER)
        # 视频是唯一可能是**单个** ref 的类型: 形态要保持
        self.assertIsInstance(back.media, VideoRef)
        self.assertEqual(back.media.url, "https://cdn.example/v.mp4")
        self.assertEqual(back.media.duration, 93)

    def test_media_types_survive_the_roundtrip(self):
        """**核心**: 动图/实况照片不能被还原成视频或图片。

        ``MediaRef`` 没有类型判别字段, 靠字段集反推区分不了 VideoRef/AniRef ——
        所以序列化必须显式带 kind。
        """
        refs = {
            "video": VideoRef(url="https://cdn.example/v.mp4", duration=10),
            "image": ImageRef(url="https://cdn.example/i.jpg", width=100),
            "animation": AniRef(url="https://cdn.example/a.gif", duration=3),
            "live_photo": LivePhotoRef(url="https://cdn.example/l.jpg", video_url="https://cdn.example/l.mp4"),
        }
        result = MultimediaParseResult(title="t", media=list(refs.values()), content="c")

        back = _roundtrip(result)

        self.assertEqual(len(back.media), 4)
        for expected, actual in zip(refs.values(), back.media, strict=True):
            self.assertIs(type(actual), type(expected), f"{type(expected).__name__} 变了: {type(actual).__name__}")
            self.assertEqual(actual.url, expected.url)

    def test_the_kind_tag_is_what_makes_types_survivable(self):
        """自证: 把 kind 抹掉后必须**响亮地失败**, 而不是猜一个类型出来。"""
        data = result_to_cache_dict(MultimediaParseResult(title="t", media=[AniRef(url="https://c/a.gif")]))
        for item in data["media"]:
            item.pop("kind")
        with self.assertRaises(ValueError):
            result_from_cache_dict(data)

    def test_image_result_keeps_the_list_shape(self):
        result = ImageParseResult(
            title="图集",
            photo=[ImageRef(url="https://cdn.example/1.jpg"), ImageRef(url="https://cdn.example/2.jpg", ext="png")],
            content="正文",
        )
        back = _roundtrip(result)

        self.assertIsInstance(back, ImageParseResult)
        self.assertIsInstance(back.media, list)
        self.assertEqual([m.url for m in back.media], ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"])
        self.assertEqual([m.ext for m in back.media], ["jpg", "png"])

    def test_richtext_keeps_markdown_and_derives_content(self):
        """``content`` 是派生属性 —— 重建时只能喂 ``markdown_content``。

        如果重建时把原来的 content 也塞进去, ``__init__`` 会用 markdown 覆盖它
        (不报错), 于是"缓存命中的文章正文"和首次解析的不一样。
        """
        markdown = "## 标题\n\n正文一段\n\n![图](https://cdn.example/a.jpg)"
        result = RichTextParseResult(
            title="文章",
            media=[ImageRef(url="https://cdn.example/a.jpg")],
            markdown_content=markdown,
        )
        result.raw_url = "https://linux.do/t/1"

        back = _roundtrip(result)

        self.assertIsInstance(back, RichTextParseResult)
        self.assertEqual(back.markdown_content, markdown)
        # content 由 markdown 派生, 必须与"现场解析"的那份一致
        self.assertEqual(back.content, RichTextParseResult(markdown_content=markdown).content)
        self.assertEqual(back.raw_url, "https://linux.do/t/1")
        self.assertIsInstance(back.media[0], ImageRef)

    def test_no_media_roundtrips_as_none(self):
        back = _roundtrip(VideoParseResult(title="无媒体", content="c"))
        self.assertIsNone(back.media)

    def test_string_media_is_refused_rather_than_guessed(self):
        """不是媒体对象也不是序列时**不猜** (猜错就是静默发错媒体)"""
        result = RichTextParseResult(title="t", markdown_content="m")
        result.media = "https://cdn.example/x.jpg"  # 故意塞错
        with self.assertRaises(TypeError):
            result_to_cache_dict(result)

    def test_a_platform_subclass_survives_the_roundtrip(self):
        """**平台子类必须原样回来** —— 平台专属行为挂在它上面。

        实例: `PixivParseResult._do_download` 注入 `Referer`（`i.pximg.net` 按它判,
        没有就 403）。往返后若降级成 `MultimediaParseResult`, 那个覆盖不再执行 ——
        症状是"缓存命中时图床 403、现场解析却正常"(2026-10-05 实际发生的回归)。
        """
        from parsehub.parsers.parser.pixiv import PixivParseResult

        result = PixivParseResult(
            title="标题",
            media=[ImageRef(url="https://i.pximg.net/img/a.jpg")],
            content="正文",
        )
        back = _roundtrip(result)

        self.assertIs(type(back), PixivParseResult, "平台子类丢了 —— 平台专属下载头会失效")
        self.assertIs(type(back)._do_download, PixivParseResult._do_download)

    def test_hashtags_survive_the_roundtrip(self):
        """标签（渲染层做精确链接化要用）必须往返无损 —— 丢了就退回正则猜边界。"""
        from parsehub.types.result import RichTextParseResult, VideoParseResult

        for cls, kwargs in (
            (VideoParseResult, {"video": "https://x"}),
            (RichTextParseResult, {"markdown_content": "md"}),
        ):
            with self.subTest(result=cls.__name__):
                original = cls(**kwargs, hashtags=["FX戦士くるみちゃん", "tag2"])
                back = _roundtrip(original)
                self.assertEqual(back.hashtags, ["FX戦士くるみちゃん", "tag2"])

    def test_an_empty_hashtag_list_roundtrips_as_empty(self):
        """空列表 = "拿不到实体" ⇒ 渲染层退回正则；不能变成 None 或丢掉字段"""
        from parsehub.types.result import VideoParseResult

        back = _roundtrip(VideoParseResult(video="https://x"))
        self.assertEqual(back.hashtags, [])

    def test_hashtags_are_not_added_to_the_public_dict(self):
        """``to_dict()`` 是公开输出格式（被别的测试逐字段冻住）—— 不该多出字段"""
        from parsehub.types.result import VideoParseResult

        self.assertNotIn("hashtags", VideoParseResult(video="https://x", hashtags=["a"]).to_dict())

    def test_the_hashtag_field_is_in_the_cache_format(self):
        """但缓存格式里有 —— 否则往返就丢了"""
        from parsehub.types.result import VideoParseResult

        data = result_to_cache_dict(VideoParseResult(video="https://x", hashtags=["a"]))
        self.assertEqual(data["hashtags"], ["a"])

    def test_the_impl_name_is_recorded(self):
        """缓存字典要带上具体类名（按 PostType 重建只能得到通用类）"""
        from parsehub.parsers.parser.pixiv import PixivParseResult

        data = result_to_cache_dict(PixivParseResult(title="t"))
        self.assertEqual(data["impl"], "PixivParseResult")
        self.assertEqual(data["type"], "multimedia")  # PostType 仍然照旧

    def test_every_platform_subclass_roundtrips(self):
        """把**所有已注册的平台子类**都过一遍往返 —— 任何一个降级都是隐患"""
        from parsehub.types.serialize import _all_result_classes

        base = {
            VideoParseResult: {"title": "t"},
            ImageParseResult: {"title": "t"},
            MultimediaParseResult: {"title": "t"},
            RichTextParseResult: {"title": "t", "markdown_content": "m"},
        }
        checked = 0
        for name, cls in _all_result_classes().items():
            if not cls.__module__.startswith("parsehub.parsers.parser."):
                continue  # 只看平台子类
            kwargs = next(
                (kw for parent, kw in base.items() if issubclass(cls, parent) and parent is not cls),
                None,
            )
            if kwargs is None:
                continue
            # 有些类**故意**不能重建 (yt-dlp 系的必填 dl), 那类只要求"不崩、不丢缓存"
            try:
                instance = cls(**kwargs)
            except TypeError:
                continue  # 需要运行期句柄的类不参与往返 (另有专门的兜底测试)

            with self.subTest(platform_result=name):
                back = _roundtrip(instance)
                # 能构造的类必须**原样**回来: 降级成别的类 = 挂在这个类上的下载/渲染行为失效
                self.assertIsInstance(back, cls, f"{name} 降级成了 {type(back).__name__}")
                checked += 1
        self.assertGreater(checked, 5, f"只检查到 {checked} 个平台子类, 断言没覆盖到位")

    def test_a_class_that_needs_runtime_state_is_refused(self):
        """构造需要**运行期句柄**的结果类 —— **必须拒绝重建**。

        这类类的典型是"必须传入解析现场才有的对象才能构造"（历史上 yt-dlp 系的
        ``YtVideoParseResult`` 必填 `dl`；那套类已随 yt-dlp 移除，这里用测试自建的类
        钉住**机制本身**）。缓存里不可能有那种对象，硬要重建只能拿到行为不同的对象。

        2026-10-06 实测的故障（就是这个"退回通用类"造成的）: YouTube 缓存命中后退成
        ``VideoParseResult``, 它没有 yt-dlp 的下载实现, 于是走基类的分片下载器去下
        ``VideoRef.url`` —— 那是 ``www.youtube.com/shorts/...`` 的**页面 URL**,
        下回来 1.2MB HTML, 产物不是媒体, 媒体处理阶段 ``ffprobe failed to get container: {}``。

        抛出去让调用方重新解析（``services/cache.py`` 会删掉这条缓存并按未命中处理）,
        行为才与现场解析一致。旧注释担心"抛了等于缓存永远读不出来"—— 读不出来**不是故障**,
        拿错对象去下载才是。
        """
        data = result_to_cache_dict(VideoParseResult(title="t", content="c"))
        data["impl"] = _NeedsRuntimeState.__name__
        with self.assertRaises(ResultRebuildUnavailable):
            result_from_cache_dict(data)

    def test_an_unknown_impl_falls_back_to_the_generic_class(self):
        """平台子类被移除后 (老缓存) 退回通用类, 而不是抛异常"""
        data = result_to_cache_dict(VideoParseResult(title="t", content="c"))
        data["impl"] = "SomeRemovedPlatformResult"
        back = result_from_cache_dict(data)
        self.assertIsInstance(back, VideoParseResult)
        self.assertEqual(back.title, "t")

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(ValueError):
            result_from_cache_dict({"type": "brand_new_type"})

    def test_an_unknown_platform_degrades_to_none(self):
        """平台枚举变了 (老缓存) 不该抛异常 —— 退化成拿不到来源即可"""
        data = result_to_cache_dict(VideoParseResult(title="t", content="c"))
        data["platform"] = "platform_that_was_removed"
        self.assertIsNone(result_from_cache_dict(data).platform)

    def test_the_dict_is_json_serializable(self):
        """缓存里存的是字符串 —— 不可 JSON 化就等于写不进去"""
        result = MultimediaParseResult(
            title="t",
            media=[VideoRef(url="https://c/v.mp4", duration=1), AniRef(url="https://c/a.gif")],
            content="c",
            published_at=PUBLISHED,
        )
        json.dumps(result_to_cache_dict(result))

    def test_polls_survive_the_roundtrip(self):
        """**核心**: 投票的结构化数据必须往返无损 —— 漏了它缓存命中时正文里的
        ``{{poll:N}}`` 占位符就找不到数据 (投票静默消失)。

        ``polls`` 同时进 ``to_dict()`` (公开格式, 被 test_core_offline 冻住) 与
        缓存格式, 这里按缓存那条路往返。
        """
        from parsehub.types.result import MultimediaParseResult, Poll, PollOption

        original = MultimediaParseResult(
            content="正文\n\n{{poll:0}}\n\n{{poll:1}}",
            polls=[
                Poll(
                    options=[PollOption(text="选项|甲", count=71), PollOption(text="乙", count=None)],
                    total_votes=71,
                    voters=222,
                    title="标题",
                    is_finished=True,
                ),
                Poll(options=[PollOption(text="只有文字")]),
            ],
        )
        back = _roundtrip(original)

        self.assertEqual(len(back.polls), 2)
        self.assertEqual(type(back.polls[0]), Poll)
        self.assertEqual(type(back.polls[0].options[0]), PollOption)
        self.assertEqual(back.polls[0].options[0].text, "选项|甲")
        self.assertEqual(back.polls[0].options[0].count, 71)
        # None (平台不给单项票数) 不能被压成 0
        self.assertIsNone(back.polls[0].options[1].count)
        self.assertEqual(back.polls[0].total_votes, 71)
        self.assertEqual(back.polls[0].voters, 222)
        self.assertEqual(back.polls[0].title, "标题")
        self.assertIs(back.polls[0].is_finished, True)
        # 第二条: 全部默认值也要原样回来
        self.assertEqual(back.polls[1].options[0].text, "只有文字")
        self.assertIsNone(back.polls[1].options[0].count)
        self.assertIsNone(back.polls[1].total_votes)
        self.assertIsNone(back.polls[1].voters)
        self.assertEqual(back.polls[1].title, "")
        self.assertIs(back.polls[1].is_finished, False)

    def test_an_empty_poll_list_roundtrips_as_empty(self):
        """普通结果 (无投票) 的 ``polls`` 往返后仍是空列表 (渲染层据此跳过替换)"""
        from parsehub.types.result import MultimediaParseResult

        back = _roundtrip(MultimediaParseResult(content="正文"))
        self.assertEqual(back.polls, [])

    def test_an_old_cache_entry_without_polls_still_loads(self):
        """**向后兼容**: 改动之前写下的缓存条目没有这个键 —— 取默认空列表, 不是崩掉"""
        from parsehub.types.result import VideoParseResult

        data = result_to_cache_dict(VideoParseResult(title="t", content="c"))
        data.pop("polls")
        back = result_from_cache_dict(data)
        self.assertEqual(back.polls, [])

    def test_the_polls_do_not_disturb_the_other_fields(self):
        """回归: 加字段不能把邻居弄丢（媒体计数 / 位置标记 / 引用角色）"""
        from parsehub.types.result import MultimediaParseResult, Poll, PollOption

        result = MultimediaParseResult(
            content="c",
            quoted_media_count=2,
            reply_media_count=1,
            position_label="#4",
            quote_roles=["reply"],
            polls=[Poll(options=[PollOption(text="A", count=1)], total_votes=1)],
        )
        rebuilt = result_from_cache_dict(result_to_cache_dict(result))
        self.assertEqual((rebuilt.quoted_media_count, rebuilt.reply_media_count), (2, 1))
        self.assertEqual(rebuilt.position_label, "#4")
        self.assertEqual(rebuilt.quote_roles, ["reply"])
        self.assertEqual(rebuilt.polls[0].options[0].text, "A")

    def test_post_type_is_preserved(self):
        for result in (
            VideoParseResult(title="t"),
            ImageParseResult(title="t"),
            MultimediaParseResult(title="t"),
            RichTextParseResult(title="t"),
        ):
            with self.subTest(type=result.type):
                back = _roundtrip(result)
                self.assertEqual(back.type, result.type)
                self.assertIs(type(back), type(result))


if __name__ == "__main__":
    unittest.main()
