"""bilibili 评论链接: 评论是正文，视频是它引用的对象（引用卡片）。

链接形如 ``…/video/BVxxx?comment_on=1&comment_root_id=<rpid>#reply<rpid>``。
三个关键点被测试钉死：

1. ``get_raw_url`` 必须**保留** ``comment_root_id`` —— 父类的白名单会剥掉未知参数，
   剥掉之后 ``_do_parse`` 看不到它，评论分支永远走不到（症状是视频正常、评论静默消失）。
2. 回复链最多 **2 个节点**（本人 + 被回复者），不翻楼中楼列表 —— ``root.replies``
   是平铺的一页（可能 18 条，混着直接回复顶层和回复楼中楼的），不是回复关系。
3. **主次不能反**：用户发的链接指向一条评论，所以作者/点赞换成评论者的、视频退居
   引用卡片（``quote_roles=['reply']``，排在正文前，走 ``reply_media_count`` 通道）。
   早期实现把视频当主体、评论当引用块，用户当场指出"显示逻辑反了"。
"""

import asyncio

from parsehub.parsers.parser.bilibili import BiliParse
from parsehub.provider_api.bilibili import BiliReply

COMMENT_URL = (
    "https://www.bilibili.com/video/BV1oxxY69Es4"
    "?comment_on=1&comment_root_id=318640242161#reply318640242161"
)
NESTED_RPID = "318654028433"
PARENT_RPID = "318641041345"


def _reply(rpid: str, parent: str, root: str = "318640242161", **over) -> dict:
    data = {
        "rpid": int(rpid),
        "rpid_str": rpid,
        "parent": int(parent),
        "parent_str": parent,
        "root": int(root),
        "root_str": root,
        "floor": 29,
        "ctime": 1793347881,
        "like": 10745,
        "rcount": 177,
        "member": {"uname": "今井_有文", "mid": 3546624783},
        "content": {"message": "唉我去，这法国v圣怎么这么坏啊", "pictures": []},
        "reply_control": {"location": "IP属地：日本"},
    }
    data.update(over)
    return data


def test_comment_rpid_is_read_from_url():
    assert BiliParse._comment_rpid(COMMENT_URL) == "318640242161"


def test_comment_rpid_ignores_non_comment_urls():
    """普通视频链接没有这个参数 —— 不能误判成评论链接。"""
    assert BiliParse._comment_rpid("https://www.bilibili.com/video/BV1oxxY69Es4") == ""
    # 值不是数字时也不能当成有效 rpid
    assert BiliParse._comment_rpid("https://www.bilibili.com/video/BV1?comment_root_id=abc") == ""


def test_get_raw_url_keeps_comment_root_id():
    """归一化后必须还看得到 comment_root_id，否则评论分支进不去。"""
    raw = asyncio.run(BiliParse().get_raw_url(COMMENT_URL))
    assert "comment_root_id=318640242161" in raw
    assert BiliParse._comment_rpid(raw) == "318640242161"


def test_get_raw_url_drops_tracking_params_but_keeps_comment():
    url = COMMENT_URL.replace("comment_on=1&", "comment_on=1&spm_id_from=333.999&")
    raw = asyncio.run(BiliParse().get_raw_url(url))
    assert "spm_id_from" not in raw
    assert "comment_root_id=318640242161" in raw


def test_reply_parses_pictures_and_location():
    data = _reply(
        "1", "0", root="0",
        content={
            "message": "带图的评论",
            "pictures": [
                {"img_src": "http://i0.hdslb.com/a.jpg", "img_width": 1080, "img_height": 578}
            ],
        },
    )
    r = BiliReply.parse(data)
    assert r.content == "带图的评论"
    assert r.location == "日本"          # "IP属地：" 前缀被剥掉
    assert len(r.images or []) == 1
    # http 要升级成 https，否则下载层可能拿到不安全的地址
    assert (r.images or [])[0].url.startswith("https://")
    assert r.like_count == 10745


def test_reply_chain_walks_parent_at_most_two_nodes():
    """楼中楼 → 取它 + 它回复的那条，然后停（不追祖父、不翻列表）。"""
    calls: list[str] = []
    by_rpid = {
        NESTED_RPID: _reply(NESTED_RPID, PARENT_RPID),
        PARENT_RPID: _reply(PARENT_RPID, "318640242161"),
    }

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class FakeClient:
        async def get(self, _url, params=None, cookies=None):
            rpid = str((params or {}).get("root"))
            calls.append(rpid)
            root = by_rpid.get(rpid)
            if not root:
                return FakeResp({"code": 0, "data": {}})
            return FakeResp({"code": 0, "data": {"root": root}})

    from parsehub.provider_api.bilibili import BiliAPI

    api = BiliAPI()
    api._client = FakeClient()  # type: ignore[assignment]

    chain = asyncio.run(api.get_reply_chain("117528055514733", NESTED_RPID))
    # 顺序从被回复者到目标，供渲染时"被回复的在上、目标在下"
    assert [r.rpid for r in chain] == [PARENT_RPID, NESTED_RPID]
    assert calls == [NESTED_RPID, PARENT_RPID]


def test_reply_chain_stops_when_parent_is_zero():
    """顶层评论（parent=0）自己就到底了，不许多打一次空请求。"""
    calls: list[str] = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class FakeClient:
        async def get(self, _url, params=None, cookies=None):
            calls.append(str((params or {}).get("root")))
            return FakeResp({"code": 0, "data": {"root": _reply("318640242161", "0", root="0")}})

    from parsehub.provider_api.bilibili import BiliAPI

    api = BiliAPI()
    api._client = FakeClient()  # type: ignore[assignment]

    chain = asyncio.run(api.get_reply_chain("117528055514733", "318640242161"))
    assert [r.rpid for r in chain] == ["318640242161"]
    assert calls == ["318640242161"]


def test_reply_chain_tolerates_deleted_comment():
    """评论被删（code != 0）时链断掉返回已取到的部分，不抛异常拖垮视频解析。"""

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class FakeClient:
        async def get(self, _url, params=None, cookies=None):
            if str((params or {}).get("root")) == NESTED_RPID:
                return FakeResp({"code": 0, "data": {"root": _reply(NESTED_RPID, PARENT_RPID)}})
            return FakeResp({"code": 404, "message": "评论不存在"})

    from parsehub.provider_api.bilibili import BiliAPI

    api = BiliAPI()
    api._client = FakeClient()  # type: ignore[assignment]

    chain = asyncio.run(api.get_reply_chain("117528055514733", NESTED_RPID))
    assert [r.rpid for r in chain] == [NESTED_RPID]


def test_video_card_links_to_video_page_not_cover():
    """引用卡片的标题要链到视频页；链到封面 jpg 会让点标题去开一张图。"""

    class FakeVideo:
        title = "没错，re0的小碎步二原是我们画的"
        author_name = "璟炎文化"
        author_url = "https://space.bilibili.com/3546624783551347"

    card = BiliParse._render_video_card(
        FakeVideo(), "https://www.bilibili.com/video/BV1oxxY69Es4"
    )
    assert 'href="https://www.bilibili.com/video/BV1oxxY69Es4"' in card
    assert ".jpg" not in card
    assert "{{media:0}}" in card      # 封面的媒体占位符在卡片里
    assert "璟炎文化" in card          # UP 主署名


def test_video_card_without_title_still_renders_cover():
    """无标题的视频（少见）也要能出卡片，不能因为标题空就整块丢掉。"""

    class FakeVideo:
        title = ""
        author_name = "某人"
        author_url = ""

    card = BiliParse._render_video_card(FakeVideo(), "https://www.bilibili.com/video/BV1")
    assert "{{media:0}}" in card


def test_video_sits_at_the_end_of_media_so_the_quote_slice_gets_it():
    """视频必须排媒体列表**末尾**：渲染层按 reply_media_count 从**尾部**切。

    视频放开头时，切出来的"引用媒体"是最末那张评论图 —— 用户报「有一张评论图
    跑到视频引用里去了」。这里用假 API 跑通整个 _parse_with_comment，钉住顺序。
    """
    target = BiliReply.parse(
        _reply(
            NESTED_RPID, PARENT_RPID,
            content={
                "message": "目标评论",
                "pictures": [
                    {"img_src": f"https://i0.hdslb.com/{i}.jpg", "img_width": 1, "img_height": 1}
                    for i in range(2)
                ],
            },
        )
    )
    parent = BiliReply.parse(_reply(PARENT_RPID, "0", root="0"))

    class FakeVideoResult:
        title = "视频标题"
        content = ""
        author_name = "UP主"
        author_url = "https://space.bilibili.com/1"
        published_at = None
        like_count = None
        media = object()          # 占位，下面替换成真 VideoRef
        reply_media_count = 0
        quoted_media_count = 0
        quote_roles = None

    class FakeVideoRef:
        duration = 16
        thumb_url = "https://i0.hdslb.com/cover.jpg"

        def __init__(self):
            self.url = "https://cdn/x.mp4"

    result = FakeVideoResult()
    result.media = FakeVideoRef()

    class FakeAPI:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_video_info(self, *_a, **_k):
            return {"data": {"View": {"aid": 1}}}

        async def get_reply_chain(self, *_a, **_k):
            return [parent, target]

    parser = BiliParse()

    async def fake_bili_api_parse(_url):
        return result

    async def run():
        parser.bili_api_parse = fake_bili_api_parse  # type: ignore[method-assign]
        import parsehub.parsers.parser.bilibili as mod

        orig = mod.BiliAPI
        mod.BiliAPI = lambda **_k: FakeAPI()  # type: ignore[assignment]
        try:
            return await parser._parse_with_comment("https://www.bilibili.com/video/BV1", NESTED_RPID)
        finally:
            mod.BiliAPI = orig  # type: ignore[assignment]

    out = asyncio.run(run())
    media = list(out.media)
    # 顺序：评论图在前，视频在末尾
    assert isinstance(media[-1], FakeVideoRef), (
        "视频必须是最后一个媒体，否则 reply 切片会切到评论图"
    )
    assert all(not isinstance(m, FakeVideoRef) for m in media[:-1])
    assert out.reply_media_count == 1
    # 卡片占位符指向视频的索引
    assert f"{{{{media:{len(media) - 1}}}}}" in out.content
