"""linux.do 楼层：主请求取主题端点，缺的层按 post id 精确取。

用户报障与逐轮纠正（每一条都成立）::

    1. 「/t/2989140/24 怎么没有主楼了？」      -> 楼层窗口不含主楼（且是静默跳过）
    2. 「为什么4楼可以？」                      -> 窗口起点 = max(1, n-5)，1~6 楼恰含主楼
    3. 「我就不信没有querystring能筛选」        -> 有: print=true / page / post_ids[]
    4. 「你这样还不如第二次请求1.json？」        -> print 是限流端点，单层 4.5KB 就够
    5. 「改成只请求post」                       -> 统一走 posts.json?post_ids[], 不再用楼层窗口

**现在的取法**::

    主请求 : /t/<topic_id>.json            -> 主题元数据 + stream + 前 20 层（1..20, 主楼在内）
    缺的层 : posts.json?post_ids[]=<id>     -> 只回那一层（实测 4.5KB）
    不用   : /t/<id>/<n>.json               -> 以该层为中心的 20 层窗口（50KB+，且不含主楼）
             /t/<id>/<n>.json?print=true    -> 打印端点，会被限流

**为什么主楼不再是问题**：主题端点总给**前 20 层**，主楼一定在里面 —— 不管分享的是第几楼。
需要另取的只剩**目标层本身**（可能是第 24、50 楼）和**被回复的层**（也可能在 20 楼之后）。

fixture（都是话题 2989140 的真实响应）::

    linuxdo_floor_4.json   1..20 层 + stream   -> 当"主题端点的响应"
    linuxdo_floor_24.json  19..38 层 + stream  -> 提供更长的 stream，以及 24/26/34 楼的数据
"""

import asyncio
import json
from pathlib import Path

from parsehub.provider_api import linuxdo as linuxdo_mod
from parsehub.provider_api.linuxdo import LinuxDoError, LinuxDoTopic

FIXTURES = Path(__file__).parent / "fixtures"
TOPIC_ID = "2989140"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _posts(payload: dict) -> list[dict]:
    return (payload.get("post_stream") or {}).get("posts") or []


def _floors(payload: dict) -> list[int]:
    return [p.get("post_number") for p in _posts(payload)]


def _topic() -> dict:
    """主题端点的响应：**前 20 层**（1..20，主楼在内）+ 完整 stream。

    stream 用 24 楼那份（更长），好让 24 / 26 / 34 楼都能查到 id。
    """
    topic = _fixture("linuxdo_floor_4.json")
    topic["post_stream"]["stream"] = (_fixture("linuxdo_floor_24.json").get("post_stream") or {}).get("stream") or []
    return topic


def _layer(floor: int) -> dict:
    """话题里某一层，包成"按 id 取单层"那种响应。"""
    for post in _posts(_fixture("linuxdo_floor_24.json")):
        if post.get("post_number") == floor:
            return {"post_stream": {"posts": [post]}}
    return {"post_stream": {"posts": []}}


def _with_reply(payload: dict, floor: int, reply_to: int) -> dict:
    """手工设某层的 reply_to（fixture 里是抓取当时的真实值，需要别的组合时用这个）"""
    for post in _posts(payload):
        if post.get("post_number") == floor:
            post["reply_to_post_number"] = reply_to
    return payload


# ---------------------------------------------------------------- 取哪些层（纯函数）


def test_the_opening_post_is_always_in_the_topic_response():
    """**核心**: 主题端点给前 20 层 ⇒ 主楼**总在手**，不用为它再发请求。

    这才是"分享第 24 楼时主楼丢了"的根治点：不是补取主楼，而是**换掉主请求端点**。
    """
    assert 1 in _floors(_topic())


def test_an_early_floor_needs_nothing():
    """分享前 20 层里的楼层 → 目标层与主楼都在手"""
    posts = _posts(_topic())
    assert LinuxDoTopic._missing_context_floors(posts, wanted=4, reply_to=None) == []
    assert LinuxDoTopic._missing_context_floors(posts, wanted=15, reply_to=None) == []


def test_the_opening_post_is_never_requested_for_itself():
    """解析主楼本身时不取主楼（不自我引用）"""
    assert LinuxDoTopic._missing_context_floors(_posts(_topic()), wanted=1, reply_to=None) == []


def test_a_replied_floor_after_twenty_is_missing():
    """被回复的层在 20 楼之后 → 不在手，要按 id 取"""
    assert LinuxDoTopic._missing_context_floors(_posts(_topic()), wanted=34, reply_to=26) == [26]


def test_a_replied_floor_within_twenty_is_not_requested():
    """被回复的层在前 20 层里 → 已在手"""
    assert LinuxDoTopic._missing_context_floors(_posts(_topic()), wanted=10, reply_to=7) == []


def test_replying_to_the_opening_post_does_not_duplicate_it():
    """回复主楼时（reply_to=1）主楼已在手，不重复算"""
    assert LinuxDoTopic._missing_context_floors(_posts(_topic()), wanted=34, reply_to=1) == []


def test_the_wanted_floor_comes_from_the_url():
    assert LinuxDoTopic._wanted_floor("24") == 24
    assert LinuxDoTopic._wanted_floor("") == 1


# ---------------------------------------------------------------- 楼层 → id


def test_the_stream_gives_the_id_for_a_floor():
    """``stream`` 是话题所有可见层的 id 列表 —— 位置就是楼层号减一"""
    payload = _topic()
    stream = (payload.get("post_stream") or {}).get("stream") or []
    assert LinuxDoTopic._post_id_for_floor(payload, 1) == stream[0]
    assert LinuxDoTopic._post_id_for_floor(payload, 24) == stream[23]


def test_a_floor_past_the_stream_has_no_id():
    """stream 比楼层号短（话题删过层 / 被过滤）→ 拿不到 id"""
    payload = _topic()
    payload["post_stream"]["stream"] = [1, 2, 3]
    assert LinuxDoTopic._post_id_for_floor(payload, 24) is None


# ---------------------------------------------------------------- 网络（只有 posts.json）


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """假的 ``http.AsyncClient``：只认 ``posts.json?post_ids[]=<id>``，按 id 反查楼层。"""

    def __init__(self, *, topic: dict | None = None, wrong_floor: bool = False, status: int = 200):
        self.topic = topic or _topic()
        self.stream = (self.topic.get("post_stream") or {}).get("stream") or []
        self.wrong_floor = wrong_floor
        self.status = status
        self.requested: list[str] = []

    def __call__(self, **_kwargs):  # AsyncClient(proxy=..., cookies=..., timeout=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url: str, **_kwargs) -> _FakeResponse:
        self.requested.append(url)
        post_id = int(url.rsplit("=", 1)[-1])
        floor = self.stream.index(post_id) + 1 if post_id in self.stream else None
        if floor is None:
            return _FakeResponse({"post_stream": {"posts": []}})
        return _FakeResponse(_layer(floor + 1 if self.wrong_floor else floor), self.status)

    def ids(self) -> list[int]:
        return [int(u.rsplit("=", 1)[-1]) for u in self.requested]


def _patch_client(monkeypatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(linuxdo_mod.http, "AsyncClient", fake)


def _fill(monkeypatch, payload: dict, post_number: str, *, fake: _FakeClient | None = None) -> dict:
    client = fake or _FakeClient()
    _patch_client(monkeypatch, client)
    return asyncio.run(LinuxDoTopic._with_required_floors(payload, TOPIC_ID, post_number))


def test_a_late_floor_is_fetched_by_id(monkeypatch):
    """**核心**: 第 24 楼不在主题端点给的 20 层里 → 按 id 取它，只一次请求"""
    client = _FakeClient()
    payload = _fill(monkeypatch, _topic(), "24", fake=client)

    assert len(client.requested) == 1, f"只该取目标层: {client.requested}"
    assert "posts.json" in client.requested[0], client.requested[0]
    assert client.ids() == [client.stream[23]], client.ids()
    floors = _floors(payload)
    assert 24 in floors, "目标层没取回来"
    assert 1 in floors, "主楼本该一直在（主题端点给的）"
    assert floors == sorted(floors)


def test_a_late_reply_fetches_the_replied_floor_too(monkeypatch):
    """**核心**: 第 34 楼回复第 26 楼 → 两层都要，各一次请求"""
    topic = _with_reply(_topic(), 34, 26)  # 26 也不在前 20 层里
    client = _FakeClient(topic=topic)
    payload = _fill(monkeypatch, topic, "34", fake=client)

    assert len(client.requested) == 2, f"目标层 + 被回复层: {client.requested}"
    assert client.ids() == [client.stream[33], client.stream[25]], client.ids()
    floors = _floors(payload)
    assert 34 in floors and 26 in floors and 1 in floors


def test_a_reply_inside_the_first_twenty_costs_one_request(monkeypatch):
    """第 24 楼回复第 7 楼 → 只取目标层（被回复的层已在手）"""
    topic = _with_reply(_topic(), 24, 7)
    client = _FakeClient(topic=topic)
    _fill(monkeypatch, topic, "24", fake=client)

    assert len(client.requested) == 1, f"被回复层已在手就不取: {client.requested}"


def test_nothing_is_requested_for_an_early_floor(monkeypatch):
    """**核心回归**: 分享前 20 层里的楼层 → 零请求"""
    client = _FakeClient()
    payload = _fill(monkeypatch, _topic(), "4", fake=client)

    assert client.requested == [], f"不该发任何请求: {client.requested}"
    assert _floors(payload) == _floors(_topic())


def test_merged_floors_are_deduplicated(monkeypatch):
    """取回的层与原 payload 可能重叠 —— 按楼层去重，不出现重复的层"""
    payload = _fill(monkeypatch, _topic(), "20")  # 20 楼本就在 payload 里
    floors = _floors(payload)
    assert len(floors) == len(set(floors)), f"有重复楼层: {floors}"


def test_the_target_floor_is_fetched_before_the_context_floors(monkeypatch):
    """**顺序**: 先目标层 —— 该取哪些上下文层，要靠它的 ``reply_to_post_number`` 才知道"""
    topic = _with_reply(_topic(), 34, 26)
    client = _FakeClient(topic=topic)
    _fill(monkeypatch, topic, "34", fake=client)

    assert client.ids()[0] == client.stream[33], f"应该先取目标层: {client.ids()}"


def test_a_wrong_floor_from_the_id_request_is_dropped(monkeypatch):
    """**核心**: stream 位置漂了（话题删过层）→ 取回的是别的层，宁可不要这段上下文。

    把别人的话当成"被回复的内容"渲染进引用块，比少一段上下文糟糕得多。
    """
    client = _FakeClient(wrong_floor=True)
    payload = _fill(monkeypatch, _topic(), "34", fake=client)

    assert 34 not in _floors(payload), "取错的层不能混进来"
    assert _floors(payload) == _floors(_topic()), "原 payload 应原样保留"


def test_a_rejected_request_does_not_break_the_parse(monkeypatch):
    """请求被拒（429 / 422 等）→ 只记 warning，不抛错"""
    client = _FakeClient(status=429)
    payload = _fill(monkeypatch, _topic(), "34", fake=client)

    assert _floors(payload) == _floors(_topic()), "原 payload 应原样保留"


def test_an_absent_id_does_not_send_a_request(monkeypatch):
    """拿不到 id（stream 太短）→ 不发无用的请求，那一层就当缺"""
    payload = _topic()
    payload["post_stream"]["stream"] = []
    client = _FakeClient(topic=payload)
    _patch_client(monkeypatch, client)

    result = asyncio.run(LinuxDoTopic._with_required_floors(payload, TOPIC_ID, "24"))
    assert client.requested == [], f"没有 id 就别发: {client.requested}"
    assert 24 not in _floors(result)


# ---------------------------------------------------------------- 端到端（真实 fixture）


def test_the_floor_renders_with_the_opening_post(monkeypatch):
    """**核心**: 补齐后第 24 楼渲染出**主楼引用块** + 自己的楼层号"""
    payload = _fill(monkeypatch, _topic(), "24")
    topic = LinuxDoTopic._from_payload(payload, TOPIC_ID, post_number="24")

    assert topic.post_number == 24
    assert topic.author_handle == "chunxiaoyi"
    markdown = topic.markdown_content
    assert "> " in markdown, f"没有主楼引用块:\n{markdown[:400]}"
    assert "· #1" in markdown, "引用块里没有主楼的楼层号"
    assert "KoaIa" in markdown, "引用块里不是主楼作者"
    # 上下文层的媒体已改为**块内原位标记** ⇒ 不再声明 reply 角色
    # （声明了渲染层会去 reply 桶找媒体，那里已经是空的）
    assert topic.quote_roles == [], topic.quote_roles
    assert topic.reply_media_count == 0


def test_a_reply_renders_both_context_blocks(monkeypatch):
    """第 34 楼回复第 26 楼 → 主楼 + 被回复的层，两个引用块按「远到近」排"""
    topic = _with_reply(_topic(), 34, 26)
    payload = _fill(monkeypatch, topic, "34")
    result = LinuxDoTopic._from_payload(payload, TOPIC_ID, post_number="34")

    markdown = result.markdown_content
    assert "· #1" in markdown, "缺主楼引用块"
    # 两个上下文块；媒体走块内原位标记 ⇒ 不声明 reply 角色
    assert result.quote_roles == [], result.quote_roles
    assert "· #26" in markdown, "缺被回复楼层的引用块"
    assert markdown.index("· #1") < markdown.index("· #26"), "顺序应该是「主楼 -> 被回复的层」"


def test_a_floor_that_cannot_be_fetched_is_an_error(monkeypatch):
    """目标层始终取不到 → ``_from_payload`` 报错（不能静默换一层给用户）"""
    payload = _fill(monkeypatch, _topic(), "99")
    try:
        LinuxDoTopic._from_payload(payload, TOPIC_ID, post_number="99")
    except LinuxDoError as exc:
        assert "99" in str(exc)
    else:
        raise AssertionError("应该报错")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
