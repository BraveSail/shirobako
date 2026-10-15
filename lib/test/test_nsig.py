"""YouTube n-sig: URL 改写与求解器输出的解析（**全离线**，不联网）。

背景（2026-10-16）: 直链里的 ``n`` 是需 JS 变换的令牌 —— 原样请求会被 403，
解过后 206。所以要把解出的值写回 URL；而**只有 n 的值能改**：``sig`` 覆盖了
``sparams`` 列出的那组参数，改任何一个都 403（实测逐项替换 id/n/sig/lsig/ns/spc 全部失败）。

这里钉住两件事：
1. ``apply_solved_n`` 只动 ``n`` 的值，其余参数（含顺序）一字不动；解不出就不返回条目
2. 求解器输出的解析：真实形态是 ``responses[*].data[原 n]``
"""

import json

from parsehub.provider_api.youtube_video import apply_solved_n
from parsehub.utils import nsig

#: 真实形态的直链（截短；保留 n、sig、sparams 等签名参数以验证"只改 n"）
_REAL_URL = (
    "https://rr3---sn-oguelnzr.googlevideo.com/videoplayback"
    "?expire=1792091249&ei=EdDQarDJH7brvcAPhcjK8Q8&ip=2603%3Ac021%3A801f&id=o-AJTHjKmYngxr"
    "&itag=18&source=youtube&requiressl=yes&mh=3D&mm=18%2C26&mn=sn-oguelnzr%2Csn-aj3pm5-55"
    "&ms=aub%2Conr&mv=m&mvi=3&pl=35&n=ajVI-7V4B-E3QQ&sparams=expire%2Cei%2Cip%2Cid"
    "&sig=AE0s2JYwRQIhALBpcnM2M3o_UQUo_WwVyWebil3RHzidXdiRFp&lsig=APaTxxMwRQIgZmhcuOzezil5Bg22P94ytC"
)


# ── apply_solved_n ────────────────────────────────────────────────────────


def test_replaces_only_the_n_value():
    """只改 ``n`` 的值 —— 其余参数（含顺序、编码）逐字保持。"""
    out = apply_solved_n([_REAL_URL], {"ajVI-7V4B-E3QQ": "SOLVED_N_VALUE"})
    assert _REAL_URL in out
    new = out[_REAL_URL]

    assert "n=SOLVED_N_VALUE" in new
    assert "n=ajVI-7V4B-E3QQ" not in new
    # 除 n 外逐字相同
    assert new.replace("n=SOLVED_N_VALUE", "n=ajVI-7V4B-E3QQ") == _REAL_URL


def test_other_signed_params_are_untouched():
    """``sig`` / ``lsig`` / ``sparams`` 等签名参数一个字符都不能动。"""
    out = apply_solved_n([_REAL_URL], {"ajVI-7V4B-E3QQ": "NEW"})
    new = out[_REAL_URL]
    for keep in (
        "sig=AE0s2JYwRQIhALBpcnM2M3o_UQUo_WwVyWebil3RHzidXdiRFp",
        "lsig=APaTxxMwRQIgZmhcuOzezil5Bg22P94ytC",
        "sparams=expire%2Cei%2Cip%2Cid",
        "itag=18",
    ):
        assert keep in new


def test_unresolved_urls_are_omitted():
    """解不出 / 解出与原值相同 ⇒ **不返回条目**（调用方保持原 URL，行为不劣化）。"""
    assert apply_solved_n([_REAL_URL], {}) == {}
    assert apply_solved_n([_REAL_URL], {"ajVI-7V4B-E3QQ": "ajVI-7V4B-E3QQ"}) == {}
    assert apply_solved_n([_REAL_URL], {"别的值": "x"}) == {}


def test_url_without_n_is_ignored():
    """没有 n 参数的 URL 不参与（老视频的直链本来就没有 n）。"""
    url = "https://rr3---sn-oguelnzr.googlevideo.com/videoplayback?itag=18&sig=abc"
    assert apply_solved_n([url], {"任意": "值"}) == {}


def test_url_without_query_is_ignored():
    assert apply_solved_n(["https://example.com/plain"], {"a": "b"}) == {}


def test_batch_mixes_resolved_and_not():
    """批量：能解的进结果，不能解的不进 —— 互不影响。"""
    other = _REAL_URL.replace("n=ajVI-7V4B-E3QQ", "n=OTHER_RAW")
    out = apply_solved_n([_REAL_URL, other], {"ajVI-7V4B-E3QQ": "A"})
    assert list(out) == [_REAL_URL]
    assert "n=A" in out[_REAL_URL]


# ── 求解器输出解析（_parse_output） ────────────────────────────────────────


def test_parses_real_solver_output_shape():
    """真实输出形态: ``responses[*].data[原 n] = 解出的 n``。

    样本形状取自实测（2026-10-16）：
    ``{"type":"result","responses":[{"type":"result","data":{"<原n>":"<新n>"}}],…}``
    """
    raw = {
        "type": "result",
        "responses": [{"type": "result", "data": {"bsUYUL36NBJL2n8J": "lo1Ov9--9Nl01w"}}],
        "preprocessed_player": "…3MB…",
    }
    stdout = json.dumps(raw)
    assert nsig._parse_output(stdout, ["bsUYUL36NBJL2n8J"]) == {  # noqa: SLF001
        "bsUYUL36NBJL2n8J": "lo1Ov9--9Nl01w"
    }


def test_ignores_values_not_requested():
    """求解器可能顺带返回别的项 —— 只取**我们请求过的**原值。"""
    stdout = json.dumps({"responses": [{"data": {"requested": "new", "other": "x"}}]})
    assert nsig._parse_output(stdout, ["requested"]) == {"requested": "new"}  # noqa: SLF001


def test_tolerates_garbage_output():
    """形状变了 / 非 JSON ⇒ 返回空（调用方降级），**不抛异常**。"""
    assert nsig._parse_output("not json at all", ["a"]) == {}  # noqa: SLF001
    assert nsig._parse_output("", ["a"]) == {}  # noqa: SLF001
    assert nsig._parse_output(json.dumps({"unexpected": []}), ["a"]) == {}  # noqa: SLF001
    assert nsig._parse_output(json.dumps({"responses": [{"data": None}]}), ["a"]) == {}  # noqa: SLF001


def test_takes_last_json_line():
    """node/bun 可能先打别的日志行 —— 取**最后一个** JSON 行。"""
    stdout = 'some log line\n{"responses": [{"data": {"a": "b"}}]}\n'
    assert nsig._parse_output(stdout, ["a"]) == {"a": "b"}  # noqa: SLF001


# ── 播放器 id 提取 ────────────────────────────────────────────────────────


def test_player_id_from_assets():
    """优先取结构化字段 ``assets.js``（不是全文正则）。"""
    data = {"assets": {"js": "/s/player/21090930/player_ias.vflset/en_US/base.js"}}
    assert nsig.player_id_from_response(data) == "21090930"


def test_player_id_from_watch_html_when_assets_are_absent():
    """**watch 页的内嵌响应没有 ``assets``** —— playerId 只能从页面 HTML 里取。

    这是实际踩到的坑: 主路走 watch 页后, 只查 ``assets`` 必然拿不到 id,
    n 解不出来 → 直链下载 403（解析看着"成功", 下载却挂）。
    """
    html = (
        "<html><script>var ytInitialPlayerResponse = {\"streamingData\": {...}};</script>"
        'var cfg = {"jsUrl":"/s/player/21090930/player_ias.vflset/en_US/base.js"};'
        "</html>"
    )
    assert nsig.player_id_from_response({"streamingData": {}}, html) == "21090930"


def test_player_id_from_html_picks_the_most_frequent():
    """页面里可能有多处 ``/s/player/`` 字样 —— 取出现次数最多的（真播放器 JS）。"""
    html = (
        "/s/player/11111111/x.js"
        "/s/player/22222222/base.js"
        "/s/player/22222222/base.js"
    )
    assert nsig.player_id_from_response({}, html) == "22222222"


def test_player_id_prefers_assets_over_html():
    """两条来源都在时, 结构化字段优先（HTML 里的正则可能被别的条目带偏）。"""
    data = {"assets": {"js": "/s/player/99999999/player_ias.vflset/en_US/base.js"}}
    assert nsig.player_id_from_response(data, "/s/player/11111111/base.js") == "99999999"


def test_player_id_missing_returns_empty():
    assert nsig.player_id_from_response({}) == ""
    assert nsig.player_id_from_response({"assets": {}}) == ""
    assert nsig.player_id_from_response({"assets": {"js": "https://example.com/x.js"}}) == ""
    assert nsig.player_id_from_response({}, "<html>no player id here</html>") == ""


def test_solve_n_without_runtime_or_input_returns_empty(monkeypatch):
    """没有运行时 / 空输入 ⇒ 空映射且**不抛**（降级路径）。"""
    assert nsig.solve_n("", ["n"]) == {}
    assert nsig.solve_n("player js", []) == {}
    monkeypatch.setattr(nsig, "find_runtime", lambda: "")
    assert nsig.solve_n("player js", ["n"]) == {}


def test_find_runtime_prefers_bun(monkeypatch):
    """bun 优先、node 兜底（实测两者解出的 n 逐字符一致，bun 更快）。"""
    monkeypatch.setattr(nsig.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "node" else None)
    assert nsig.find_runtime() == "/usr/bin/node"

    monkeypatch.setattr(nsig.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert nsig.find_runtime() == "/usr/bin/bun"

    monkeypatch.setattr(nsig.shutil, "which", lambda name: None)
    assert nsig.find_runtime() == ""


def test_player_cache_roundtrip():
    """播放器 JS 按 player_id 缓存（跨内容，稳态不增请求）。"""
    nsig.reset_player_cache()
    assert nsig._player_cache == {}  # noqa: SLF001
    nsig._player_cache["123"] = (0.0, "js")  # noqa: SLF001
    assert nsig._player_cache["123"][1] == "js"  # noqa: SLF001


def test_scripts_are_available_from_the_package():
    """``yt-dlp-ejs`` 必须装得上（它是纯 JS 求解器包）。"""
    lib, core = nsig._load_scripts()  # noqa: SLF001
    assert len(lib) > 100_000, "lib 段应含 meriyah 解析器（~150KB）"
    assert "jsc" in core or "function" in core


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
