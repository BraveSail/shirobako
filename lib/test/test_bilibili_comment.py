"""bilibili 评论链接: 解析视频 + 目标评论及其被回复的评论（最多两层）。

链接形如 ``…/video/BVxxx?comment_on=1&comment_root_id=<rpid>#reply<rpid>``。
两个关键点被测试钉死：

1. ``get_raw_url`` 必须**保留** ``comment_root_id`` —— 父类的白名单会剥掉未知参数，
   剥掉之后 ``_do_parse`` 看不到它，评论分支永远走不到（症状是视频正常、评论静默消失）。
2. 回复链最多 **2 个节点**（本人 + 被回复者），不翻楼中楼列表 —— ``root.replies``
   是平铺的一页（可能 18 条，混着直接回复顶层和回复楼中楼的），不是回复关系。
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
