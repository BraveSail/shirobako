import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from _fakes import FakeResponse, patch_async_get

from parsehub import ParseHub, Platform
from parsehub.parsers.parser.bangumi import BangumiParser
from parsehub.parsers.parser.bilibili import BiliParse
from parsehub.parsers.parser.coolapk import CoolapkParser
from parsehub.parsers.parser.douban import DoubanParser
from parsehub.parsers.parser.douyin import DouyinApiResult, DouyinParser
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.parsers.parser.kuaishou import KuaiShouParser
from parsehub.parsers.parser.pipix import PipixParser
from parsehub.parsers.parser.pixiv import PixivParser
from parsehub.parsers.parser.threads import ThreadsParser
from parsehub.parsers.parser.tieba import TieBaParser
from parsehub.parsers.parser.tiktok import TikTokApiResult, TikTokParser
from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.parsers.parser.weibo import WeiboParser
from parsehub.parsers.parser.weixin import WXParser
from parsehub.parsers.parser.xhs import XHSParser
from parsehub.parsers.parser.xiaoheihe import XiaoHeiHeParser
from parsehub.parsers.parser.zhihu import ZhihuParser
from parsehub.parsers.parser.zuiyou import ZuiYouParser
from parsehub.provider_api.bangumi import BangumiBlog
from parsehub.provider_api.bilibili import BiliAPI, BiliDynamic
from parsehub.provider_api.coolapk import Coolapk
from parsehub.provider_api.douban import Douban, DoubanTopic
from parsehub.provider_api.instagram import InstagramAPI, InstagramPost
from parsehub.provider_api.kuaishou import KuaiShouAPI, KuaishouParser
from parsehub.provider_api.pipix import Pipix, PipixPost, PipixPostType
from parsehub.provider_api.pixiv import Pixiv, PixivIllust, PixivImage
from parsehub.provider_api.threads import ThreadsAPI, ThreadsPost
from parsehub.provider_api.tieba import TieBa, TieBaPost
from parsehub.provider_api.twitter import TwitterTweet
from parsehub.provider_api.weibo import Data, WeiboAPI, WeiboContent, WeiboTVContent
from parsehub.provider_api.weixin import WX, WXItemShowType
from parsehub.provider_api.xhs import XHSAPI, XHSPost, XHSPostType
from parsehub.provider_api.xiaoheihe import XiaoHeiHeAPI, XiaoHeiHePost, XiaoHeiHePostType
from parsehub.provider_api.zhihu import ZhihuAPI, ZhihuPin, ZhihuQA, ZhihuZhuanLan
from parsehub.provider_api.zuiyou import ZuiYou, ZuiYouPost
from parsehub.utils.helpers import SecretCookie, get_author_name

AUTHOR = "Sample & Author"
VIDEO = "https://cdn.example/video.mp4"


@pytest.mark.parametrize("value", [None, {}, [], 123, {"name": None}, {"name": 123}])
def test_missing_author_is_empty(value):
    assert get_author_name(value) == ""


def test_name_precedes_username_and_skips_empty_values():
    assert get_author_name({"full_name": "  " + AUTHOR + "  ", "username": "handle"}) == AUTHOR
    assert get_author_name({"full_name": "  ", "username": "handle"}) == "handle"
    assert get_author_name({"title": "Not the author", "name": None}) == ""


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (ThreadsPost.from_graphql, {"caption": {"text": "Body"}, "user": {"full_name": AUTHOR}}),
        (InstagramPost, {"owner": {"full_name": AUTHOR}}),
        (DoubanTopic.parse, {"author": {"name": AUTHOR}}),
        (
            BiliDynamic.parse,
            {"item": {"modules": {"module_author": {"name": AUTHOR}, "module_dynamic": {"desc": None}}}},
        ),
        (Data.parse, {"user": {"screen_name": AUTHOR}, "retweeted_status": {"user": {"screen_name": "Other"}}}),
        (
            TieBaPost.parse,
            {"thread": {"author": {"name_show": AUTHOR}, "origin_thread_info": {"title": "T", "content": []}}},
        ),
        (ZhihuQA.parse, {"question": {"title": "Q"}, "content": "Answer", "author": {"name": AUTHOR}}),
        (ZhihuZhuanLan.parse, {"title": "T", "content": "Body", "author": {"name": AUTHOR}}),
        (ZhihuPin.parse, {"content": [], "author": {"name": AUTHOR}}),
        (ZuiYouPost.parse, {"data": {"post": {"member": {"name": AUTHOR}}}}),
    ],
)
def test_json_providers_preserve_authors(factory, payload):
    assert factory(payload).author_name == AUTHOR


@pytest.mark.parametrize("media_type", [1, 2, 8])
def test_instagram_new_graphql_schema_keeps_owner(media_type):
    node = InstagramAPI()._convert_v1_media({"media_type": media_type, "user": {"full_name": AUTHOR}})
    assert InstagramPost(node).author_name == AUTHOR


def test_threads_username_fallback_and_linked_media_author():
    post = ThreadsPost.from_graphql(
        {
            "media_type": 19,
            "user": {"full_name": "", "username": "post_owner"},
            "text_post_app_info": {"linked_inline_media": {"media_type": 19, "user": {"full_name": "Not the owner"}}},
        }
    )
    assert post.author_name == "post_owner"


def test_threads_url_fallback():
    payload = {"data": {"data": {"edges": [{"node": {"thread_items": [{"post": {"code": "Abc"}}]}}]}}}
    with patch.object(ThreadsAPI, "_post_graphql", new=AsyncMock(return_value=payload)):
        post = asyncio.run(ThreadsAPI().parse("https://www.threads.com/@handle/post/Abc"))
    assert post.author_name == "handle"


@pytest.mark.parametrize("image_key", ["images", "image_post_info", None])
def test_douyin_image_and_video_authors(image_key):
    detail = {"author": {"nickname": AUTHOR}}
    if image_key == "images":
        detail[image_key] = [{"url_list": ["https://cdn.example/image.jpg"]}]
    elif image_key:
        detail[image_key] = {"images": []}
    else:
        detail["video"] = {"is_bytevc1": 0, "play_addr": {"url_list": [VIDEO]}}
    info = DouyinApiResult.parse({"aweme_detail": detail})
    result = DouyinParser._build_image_result(info) if image_key else DouyinParser._build_video_result(info)
    assert result.author_name == AUTHOR


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("author", [{"nickname": AUTHOR}, {"uniqueId": AUTHOR}, AUTHOR])
def test_tiktok_image_and_video_authors(image, author):
    detail = {"author": author}
    if image:
        detail["imagePost"] = {"images": [{"imageURL": {"urlList": ["https://cdn.example/image.jpg"]}}]}
    else:
        detail["video"] = {"playAddr": VIDEO}
    info = TikTokApiResult.parse(detail)
    result = TikTokParser._build_image_result(info) if image else TikTokParser._build_video_result(info)
    assert result.author_name == AUTHOR


@pytest.mark.parametrize("layout", ["horizontal", "vertical"])
def test_douban_result_branches_keep_author(layout):
    topic = DoubanTopic.parse({"author": {"name": AUTHOR}, "image_layout": layout})
    with patch.object(Douban, "parse", new=AsyncMock(return_value=topic)):
        result = asyncio.run(DoubanParser()._do_parse("https://douban.com/topic/1/"))
    assert result.author_name == AUTHOR


def test_bilibili_video_owner():
    view = {
        "cid": 1,
        "duration": 1,
        "dimension": {},
        "desc": "Body",
        "title": "T",
        "pic": "",
        "owner": {"name": AUTHOR},
    }
    with (
        patch.object(BiliAPI, "get_video_info", new=AsyncMock(return_value={"data": {"View": view}})),
        patch.object(BiliAPI, "get_buvid", new=AsyncMock(return_value=("", ""))),
        patch.object(BiliAPI, "get_video_playurl", new=AsyncMock(return_value={"data": {"durl": [{"url": VIDEO}]}})),
    ):
        result = asyncio.run(BiliParse().bili_api_parse("https://www.bilibili.com/video/BV123"))
    assert result.author_name == AUTHOR


def test_xhs_note_author():
    note = {"title": "T", "desc": "Body", "type": "normal", "user": {"nickname": AUTHOR}}
    result = XHSAPI()._XHSAPI__parse({"note": {"firstNoteId": "1", "noteDetailMap": {"1": {"note": note}}}})
    assert result.author_name == AUTHOR


def test_pipix_render_data_author():
    payload = {"ppxItemDetail": {"item": {"item_type": 1, "content": "Body", "author": {"name": AUTHOR}}}}
    post = Pipix._parse_data('<script id="RENDER_DATA">' + quote(json.dumps(payload)) + "</script>")
    assert post.author_name == AUTHOR


@pytest.mark.parametrize("video", [False, True])
def test_xiaoheihe_user(video):
    link = {
        "title": "T",
        "text": '[{"type":"text","text":"Body"}]',
        "has_video": video,
        "video_url": VIDEO,
        "video_thumb": "",
        "user": {"username": AUTHOR},
    }
    with patch.object(XiaoHeiHeAPI, "link_tree", new=AsyncMock(return_value={"link": link})):
        result = asyncio.run(XiaoHeiHeAPI().parse("https://xiaoheihe.cn/app/bbs/link/123"))
    assert result.author_name == AUTHOR


# ── 小黑盒页脚元数据（同一次 link/tree 响应里就有, 不需要额外请求）──────────────
#: 取自真实响应 lib/test/fixtures/xiaoheihe_link_tree.json（link 段的原值）
_XHH_LINK = {
    "title": "最新消息！GTA6或将于2027年第一季度登录PC",
    "text": '[{"type":"text","text":"Body"}]',
    "has_video": 0,
    "use_concept_type": 1,
    "create_at": 1791939655,
    "link_award_num": 69,
    "comment_num": 76,
    "forward_num": 0,
    "user": {"userid": 49946265, "username": "爱猪人士o"},
}


def test_xiaoheihe_footer_metadata_from_same_response():
    """页脚四段全部来自**同一次** link/tree 响应（不做第二次请求）。

    小黑盒**没有浏览量**字段（真实响应里 42 个字段全翻过, 无 view/click 计数）
    ⇒ ``view_count`` 必须是 None（「有就填、没有就 None」硬规则）。
    """
    with patch.object(XiaoHeiHeAPI, "link_tree", new=AsyncMock(return_value={"link": _XHH_LINK})):
        post = asyncio.run(XiaoHeiHeAPI().parse("https://xiaoheihe.cn/app/bbs/link/123"))

    assert post.author_id == "49946265"
    assert post.published_at == datetime.fromtimestamp(1791939655, tz=UTC)
    assert post.like_count == 69
    assert post.reply_count == 76


def test_xiaoheihe_parser_forwards_footer_metadata():
    """解析层把页脚字段**转发到 result**（作者标识走数字 uid, 同抖音/bgm 的约定）。

    漏转发的话缓存里全是 None, 而 None 与"平台没有"长得一样 —— 页脚会静默少几段。
    """
    with patch.object(XiaoHeiHeAPI, "parse", new=AsyncMock(return_value=XiaoHeiHePost(
        XiaoHeiHePostType.IMAGE,
        "T",
        content="Body",
        author_name=AUTHOR,
        author_id="49946265",
        published_at=datetime.fromtimestamp(1791939655, tz=UTC),
        like_count=69,
        reply_count=76,
    ))):
        result = asyncio.run(XiaoHeiHeParser()._do_parse("https://xiaoheihe.cn/app/bbs/link/123"))

    assert result.author_name == AUTHOR
    assert result.author_handle == "49946265"
    assert result.author_url == "https://www.xiaoheihe.cn/app/bbs/user/49946265"
    assert getattr(result, "like_count", None) == 69
    assert result.reply_count == 76
    assert getattr(result, "view_count", None) is None


def test_xiaoheihe_missing_fields_stay_none():
    """老/简化响应缺这些字段时留 None, 不能抛（页脚那几段自动省略）。"""
    link = {"title": "T", "text": '[{"type":"text","text":"Body"}]', "has_video": 0}
    with patch.object(XiaoHeiHeAPI, "link_tree", new=AsyncMock(return_value={"link": link})):
        post = asyncio.run(XiaoHeiHeAPI().parse("https://xiaoheihe.cn/app/bbs/link/123"))
    assert post.published_at is None
    assert post.like_count is None
    assert post.reply_count is None
    assert post.author_id == ""


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("nick_name: 'Publisher Name'", "Publisher Name"),
        ("author: 'Fallback Author'", "Fallback Author"),
    ],
)
def test_weixin_publisher_name(field, expected):
    html = (
        "<script>var cgiDataNew = {"
        "item_show_type: '0' * 1,"
        "title: 'T',"
        f"{field},"
        "content_noencode: '<div class=\"rich_media_content\">Body</div>'"
        "};</script>"
    )
    post = WX._parse_data(html)
    assert post.author_name == expected


@pytest.mark.parametrize(
    "body",
    [
        '<div class="feed-message">Body</div>',
        '<h1 class="message-title">T</h1><div class="feed-article-message">Body</div>',
    ],
)
def test_coolapk_author(body):
    response = FakeResponse(200, text='<a class="user-name">Author</a>' + body)
    with patch_async_get(response):
        post = asyncio.run(Coolapk.parse("https://coolapk.com/feed/1"))
    assert post.author_name == "Author"


def test_kuaishou_mobile_author():
    parser = KuaishouParser("https://v.m.chenzhongtech.com/fw/photo/123")
    parser.page_type = "ATLAS"
    parser.structured_data = {"item": {"photo": {}, "atlas": {}, "user": {"user_name": AUTHOR}}}
    assert parser.get_author_name() == AUTHOR


def test_kuaishou_apollo_author_reference():
    parser = KuaishouParser("https://www.kuaishou.com/short-video/123")
    parser.page_type = "VIDEO"
    parser.client = {
        "VisionVideoDetailPhoto:123": {"author": {"__ref": "VisionVideoDetailAuthor:1"}},
        "VisionVideoDetailAuthor:1": {"name": AUTHOR},
    }
    assert parser.get_author_name() == AUTHOR


def test_kuaishou_root_query_author_reference():
    parser = KuaishouParser("https://www.kuaishou.com/short-video/123")
    parser.page_type = "VIDEO"
    parser.client = {
        "ROOT_QUERY": {'visionVideoDetail({"photoId":"123"})': {"__ref": "detail:123"}},
        "detail:123": {"author": {"__ref": "VisionVideoDetailAuthor:1"}},
        "VisionVideoDetailAuthor:1": {"name": AUTHOR},
    }
    assert parser.get_author_name() == AUTHOR


def test_kuaishou_api_fallback_author():
    upstream = SimpleNamespace(
        page_type="VIDEO",
        get_cover_photo_url=lambda: "",
        get_title_content=lambda: "Body",
        get_real_video_url=lambda: (_ for _ in ()).throw(ValueError("HTML unavailable")),
    )
    post = SimpleNamespace(title="T", author_name=AUTHOR, video_url=VIDEO, thumb_url="", duration=1, height=1, width=1)
    with (
        patch.object(KuaishouParser, "create", new=AsyncMock(return_value=upstream)),
        patch.object(KuaiShouAPI, "get_video_info", new=AsyncMock(return_value=post)),
    ):
        result = asyncio.run(KuaiShouParser()._do_parse("https://kuaishou.com/short-video/123"))
    assert result.author_name == AUTHOR


@pytest.mark.parametrize("media_type", ["GraphImage", "GraphVideo", "GraphSidecar"])
def test_instagram_all_media_branches(media_type):
    post = InstagramPost(
        {
            "__typename": media_type,
            "is_video": media_type == "GraphVideo",
            "video_url": VIDEO,
            "display_url": "https://cdn.example/image.jpg",
            "owner": {"full_name": AUTHOR},
            "edge_sidecar_to_children": {"edges": []},
        }
    )
    with patch.object(InstagramParser, "_parse", new=AsyncMock(return_value=post)):
        result = asyncio.run(InstagramParser()._do_parse("https://instagram.com/p/123"))
    assert result.author_name == AUTHOR


@pytest.mark.parametrize("media_type", ["GraphImage", "GraphVideo", "GraphSidecar"])
def test_instagram_keeps_published_at_and_counts(media_type):
    """Instagram 的发布时间/点赞/播放要传进结果 —— 以前只传 title/content/author,
    页脚就只剩来源 (用户报「页脚只有来源」)。拿不到的项保持 None, 页脚自动跳过。
    """
    post = InstagramPost(
        {
            "__typename": media_type,
            "is_video": media_type == "GraphVideo",
            "video_url": VIDEO,
            "display_url": "https://cdn.example/image.jpg",
            "owner": {"full_name": AUTHOR},
            "edge_sidecar_to_children": {"edges": []},
            "taken_at_timestamp": 1790949945,
            "edge_media_preview_like": {"count": 348},
            "video_view_count": 12345,
        }
    )
    with patch.object(InstagramParser, "_parse", new=AsyncMock(return_value=post)):
        result = asyncio.run(InstagramParser()._do_parse("https://instagram.com/p/123"))

    assert result.like_count == 348
    assert result.view_count == 12345
    assert result.published_at is not None
    assert result.published_at.year == 2026


def test_instagram_without_stats_keeps_them_none():
    """图文没有播放数 (reel 才有) —— 保持 None, 页脚不显示那段 (绝不留空占位)"""
    post = InstagramPost(
        {
            "__typename": "GraphImage",
            "is_video": False,
            "display_url": "https://cdn.example/image.jpg",
            "owner": {"full_name": AUTHOR},
        }
    )
    with patch.object(InstagramParser, "_parse", new=AsyncMock(return_value=post)):
        result = asyncio.run(InstagramParser()._do_parse("https://instagram.com/p/123"))
    assert result.view_count is None
    assert result.like_count is None
    assert result.published_at is None


@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("gif", [False, True])
def test_coolapk_all_result_types(rich, gif):
    post = Coolapk(
        title="T",
        markdown_content="Body" if rich else "",
        text_content="Body",
        imgs=["https://cdn.example/image.gif" if gif else "https://cdn.example/image.jpg"],
        author_name=AUTHOR,
    )
    with patch.object(Coolapk, "parse", new=AsyncMock(return_value=post)):
        result = asyncio.run(CoolapkParser()._do_parse("https://coolapk.com/feed/123"))
    assert result.author_name == AUTHOR


FORWARD_CASES = [
    (BangumiParser, BangumiBlog, "parse", BangumiBlog(blog_id="1", markdown_content="Body", author_name=AUTHOR)),
    (CoolapkParser, Coolapk, "parse", Coolapk(text_content="Body", author_name=AUTHOR)),
    (DoubanParser, Douban, "parse", DoubanTopic.parse({"author": {"name": AUTHOR}})),
    (
        InstagramParser,
        InstagramParser,
        "_parse",
        InstagramPost(
            {"__typename": "GraphImage", "display_url": "https://cdn.example/image.jpg", "owner": {"full_name": AUTHOR}}
        ),
    ),
    (PipixParser, Pipix, "parse", PipixPost(PipixPostType.IMAGE, "Body", author_name=AUTHOR)),
    (
        PixivParser,
        Pixiv,
        "parse",
        PixivIllust(
            illust_id="1",
            title="T",
            author_name=AUTHOR,
            author_id="1",
            tags=[],
            description="",
            images=[PixivImage(url="https://cdn.example/image.jpg")],
            page_count=1,
            is_r18=False,
            create_date="",
        ),
    ),
    (ThreadsParser, ThreadsParser, "_parse", ThreadsPost("Body", author_name=AUTHOR)),
    (
        TieBaParser,
        TieBa,
        "parse",
        TieBaPost.parse(
            {"thread": {"origin_thread_info": {"title": "T", "content": []}, "author": {"name_show": AUTHOR}}}
        ),
    ),
    (TwitterParser, TwitterParser, "_parse", TwitterTweet(tweet_id="1", author_name=AUTHOR)),
    (WeiboParser, WeiboAPI, "parse", WeiboContent(Data(text_raw="Body", author_name=AUTHOR))),
    (WeiboParser, WeiboAPI, "parse", WeiboTVContent("Body", VIDEO, 1, "", author_name=AUTHOR)),
    (WXParser, WX, "parse", WX(WXItemShowType.ARTICLE, "T", "Body", [], "Body", [], author_name=AUTHOR)),
    (XHSParser, XHSAPI, "extract", XHSPost(XHSPostType.IMAGE, "T", "Body", author_name=AUTHOR)),
    (
        XiaoHeiHeParser,
        XiaoHeiHeAPI,
        "parse",
        XiaoHeiHePost(XiaoHeiHePostType.IMAGE, "T", content="Body", author_name=AUTHOR),
    ),
    (
        XiaoHeiHeParser,
        XiaoHeiHeAPI,
        "parse",
        XiaoHeiHePost(XiaoHeiHePostType.ARTICLE, "T", content="Body", author_name=AUTHOR),
    ),
    (ZhihuParser, ZhihuAPI, "parse", ZhihuQA(question="Q", imgs=[], author_name=AUTHOR)),
    (ZhihuParser, ZhihuAPI, "parse", ZhihuQA(question="Q", imgs=[], markdown_answer="Answer", author_name=AUTHOR)),
    (ZhihuParser, ZhihuAPI, "parse", ZhihuZhuanLan(title="T", imgs=[], author_name=AUTHOR)),
    (ZhihuParser, ZhihuAPI, "parse", ZhihuPin.parse({"content": [], "author": {"name": AUTHOR}})),
    (ZuiYouParser, ZuiYou, "parse", ZuiYouPost("Body", [], {}, author_name=AUTHOR)),
]


@pytest.mark.parametrize(("parser_type", "provider_type", "method", "post"), FORWARD_CASES)
def test_parser_forwards_author(parser_type, provider_type, method, post):
    parser = parser_type(cookie=SecretCookie({"d_c0": "test"}))
    with patch.object(provider_type, method, new=AsyncMock(return_value=post)):
        result = asyncio.run(parser._do_parse("https://example.com/p/123"))
    assert result.author_name == AUTHOR


def test_bilibili_dynamic_author():
    with patch.object(BiliParse, "get_dynamic_info", new=AsyncMock(return_value=BiliDynamic(author_name=AUTHOR))):
        result = asyncio.run(BiliParse()._do_parse("https://t.bilibili.com/1234567890123456789"))
    assert result.author_name == AUTHOR


@pytest.mark.parametrize("video", [True, False])
def test_kuaishou_html_result_author(video):
    upstream = SimpleNamespace(
        page_type="VIDEO" if video else "ATLAS",
        get_author_name=lambda: AUTHOR,
        get_cover_photo_url=lambda: "https://cdn.example/image.jpg",
        get_title_content=lambda: "Body",
        get_real_video_url=lambda: SimpleNamespace(url=VIDEO, d=1, h=1, w=1),
        get_image_list=lambda: [],
    )
    with patch.object(KuaishouParser, "create", new=AsyncMock(return_value=upstream)):
        result = asyncio.run(KuaiShouParser()._do_parse("https://kuaishou.com/fw/photo/123"))
    assert result.author_name == AUTHOR


def test_all_registered_platforms_have_author_coverage():
    platforms = {case[0].__platform__ for case in FORWARD_CASES}
    platforms.update(
        {
            Platform.BILIBILI,
            Platform.DOUYIN,
            Platform.TIKTOK,
            Platform.KUAISHOU,
            Platform.FACEBOOK,
            Platform.YOUTUBE,
            Platform.SNAPCHAT,
            Platform.LINUXDO,
            # PTT 的作者解析在 test_ptt.py::test_metaline_fields（无转发，不在 FORWARD_CASES）
            Platform.PTT,
        }
    )
    assert platforms == {p.__platform__ for p in ParseHub().parsers}
