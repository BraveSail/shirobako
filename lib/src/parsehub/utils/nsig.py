"""YouTube 的 ``n`` 签名（n-sig）解算 —— 走 ``yt-dlp-ejs`` 的 JS 求解器 + node。

## 为什么需要它

YouTube 的媒体直链形如
``https://…googlevideo.com/videoplayback?…&n=<令牌>&…``，其中 ``n`` 是个**需要 JS 变换**
的令牌：拿响应里原样的 ``n`` 去请求会被 ``403``。这一步（俗称 n-sig descrambling）
不是加密校验，而是**服务端要求客户端跑一段播放器 JS**——网页版一直在做。

实测（2026-10-16，同一条视频、同一出口、同一 cookie）：

| URL 里的 n | 结果 |
| --- | --- |
| 响应原样给的 | **403** |
| 经本模块解算后的 | **206 / 可下载** |

⇒ 不接这一步，新视频会表现为 ``LOGIN_REQUIRED`` / 直链 403，而不是"画质问题"。

## 机制与依赖

- 求解器是**纯 JS**（``yt-dlp-ejs`` 包，Unlicense），由 ``lib.min.js`` + ``core.min.js`` 两段组成；
  后者内含 meriyah（JS 解析器），所以**在任意 JS 引擎里自包含可跑**。
- **要有 JS 运行时**：本模块**优先用 ``bun``、回退 ``node``**（与上游 yt-dlp 同款做法：
  stdin 喂脚本、stdout 收 JSON）。两者实测解出的 n **逐字符一致**，bun 冷启动更快
  （0.9s vs 1.2s）且是**单个静态二进制**（84MB，无需包管理器/权限参数）。
  运行时缺失时**不抛错**，返回空映射让调用方走降级（拿不到明文流时表现为解析失败，
  而不是整个 YouTube 平台不可用）。
- 输入是**播放器 JS 源码**（~3MB，从 ``/s/player/<id>/…/base.js`` 取）。
  它按 **playerId** 跨进程缓存（与 ``_visitor_cache`` 同一手法）——
  n 变换函数由播放器版本决定，同一版本对所有视频通用，所以缓存是**跨内容**的，
  稳态下不增加每次解析的请求数（符合项目"辅助请求必须替换或做跨内容缓存"的硬规则）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any

from loguru import logger

logger = logger.bind(name="NSig")

#: 求解器脚本加载失败时的提示（``yt-dlp-ejs`` 是必装依赖，缺了说明装错了）
_MISSING_EJS_HINT = "缺少依赖 yt-dlp-ejs —— 它是纯 JS 求解器包（pip install yt-dlp-ejs）"

#: JS 运行时候选，**按优先级排列**（实测两者解出的 n 完全一致）。
#:
#: ``bun`` 优先: 单文件静态二进制（84MB）、冷启动更快（0.9s vs 1.2s）、
#: 不需要 node 那套 ``--permission`` 参数。``node`` 作为通用兜底。
_RUNTIME_CANDIDATES = ("bun", "node")

#: 求解器单次执行超时（秒）。播放器 JS 有 3MB，解析要几秒。
_TIMEOUT = 180


def find_runtime() -> str:
    """返回可用的 JS 运行时路径（找不到返回空串）。"""
    for name in _RUNTIME_CANDIDATES:
        if path := shutil.which(name):
            return path
    return ""


def node_available() -> bool:
    """JS 运行时是否可用（缺了就静默降级）。

    名字保留 ``node_available`` 是给调用方一个稳定的探测入口，
    实际判断的是"任一候选运行时可用"。
    """
    return bool(find_runtime())


def _load_scripts() -> tuple[str, str]:
    """取求解器的两段 JS（lib + core）。缺依赖时抛 RuntimeError。"""
    try:
        from yt_dlp_ejs.yt import solver  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        raise RuntimeError(_MISSING_EJS_HINT) from exc
    return solver.lib(), solver.core()


def _build_stdin(player_js: str, challenges: list[str]) -> str:
    """拼出喂给 node 的脚本（形态与上游 yt-dlp 的 ``_construct_stdin`` 一致）。

    上半段把 ``lib`` 挂到 globalThis（core 依赖它），下半段调用 ``jsc(...)`` 并把
    结果打成一行 JSON。**必须 ``output_preprocessed: True``** —— 否则每次都要重新
    解析 3MB 播放器 JS（我们把 preprocessed 的播放器存下来复用）。
    """
    lib, core = _load_scripts()
    payload: dict[str, Any] = {
        "type": "player",
        "player": player_js,
        "output_preprocessed": True,
        "requests": [{"type": "n", "challenges": challenges}],
    }
    return (
        f"{lib}\n"
        "Object.assign(globalThis, lib);\n"
        f"{core}\n"
        f"console.log(JSON.stringify(jsc({json.dumps(payload)})));\n"
    )


def solve_n(player_js: str, challenges: list[str]) -> dict[str, str]:
    """把**未解的 n** 批量解成有效值。返回 ``{原值: 解出的值}``。

    解不出来的项**不出现在返回值里**（调用方对缺失项保持原样，行为不劣化）。
    任何环境/执行错误都只记日志并返回 ``{}`` —— n-sig 解不了应当是"拿不到明文流"，
    而不是让整个解析抛异常。
    """
    challenges = [c for c in challenges if c]
    if not challenges or not player_js:
        return {}
    runtime = find_runtime()
    if not runtime:
        logger.debug("没有可用的 JS 运行时(bun/node), 跳过 n-sig 解算")
        return {}

    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令 + stdin 传参, 不拼 shell
            [runtime, "-"],
            input=_build_stdin(player_js, challenges),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"n-sig 解算执行失败: {type(exc).__name__}: {exc}")
        return {}

    if proc.returncode != 0:
        logger.warning(f"n-sig 解算退出码 {proc.returncode}: {proc.stderr[-300:]}")
        return {}

    return _parse_output(proc.stdout, challenges)


def _parse_output(stdout: str, challenges: list[str]) -> dict[str, str]:
    """解析求解器输出。

    形态（实测）::

        {"type":"result",
         "responses":[{"type":"result","data":{"<原 n>":"<解出的 n>"}}],
         "preprocessed_player":"…"}

    只取 ``responses[*].data`` 里**原值命中**的项；shape 变了就返回空（下次照样降级）。
    """
    payload: dict[str, Any] | None = None
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
    if not payload:
        logger.debug("n-sig 求解器没有输出 JSON")
        return {}

    solved: dict[str, str] = {}
    wanted = set(challenges)
    for item in payload.get("responses") or []:
        data = item.get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            continue
        for original, value in data.items():
            if original in wanted and isinstance(value, str) and value:
                solved[original] = value
    return solved


# ── 播放器 JS（按 playerId 跨进程缓存） ────────────────────────────────────

_PLAYER_URL = "https://www.youtube.com/s/player/{player_id}/player_ias.vflset/en_US/base.js"
"""播放器 JS 的地址（``player_id`` 从 player 响应的 ``assets.js`` 里取）。

``player_ias`` 是上游 yt-dlp 用的变体；语言段用 ``en_US``（与它一致，
不需要 zh_CN —— n 变换与语言无关）。**不换域名**。
"""

#: 播放器 JS 的进程内缓存：``player_id -> (取到的时间, JS 源码)``。
#:
#: n 变换函数**只由播放器版本决定**（对所有视频通用），所以这是**跨内容**缓存 ——
#: 稳态下每个播放器版本只取一次。TTL 取 6 小时：播放器版本换得不勤，
#: 但换版后旧 JS 会算出无效 n，不能长期缓存。
_PLAYER_TTL = 6 * 3600
_player_cache: dict[str, tuple[float, str]] = {}

#: 从 player 响应里取播放器版本（``assets.js`` 形如 ``/s/player/<id>/...``）
_JS_ID_RE = re.compile(r"/s/player/([0-9a-zA-Z_-]+)/")


def player_id_from_response(data: Any, html: str = "") -> str:
    """取播放器 id（纯函数；取不到返回空串）。

    **两条来源都要看**（2026-10-15 实测）:

    1. ``data["assets"]["js"]`` —— innertube ``POST player`` 响应的字段。
    2. **watch 页 HTML 里的播放器 JS 地址** —— 内嵌的 ``ytInitialPlayerResponse``
       **没有 ``assets``**（实测: 30 条流、无 assets），所以只查它必然拿不到 id，
       整条 n-sig 链就断了（表现: 直链下载 403）。

    先问结构化字段，再退回 HTML 里的 ``/s/player/<id>/`` 字样。
    """
    assets = data.get("assets") if isinstance(data, dict) else None
    if isinstance(assets, dict):
        js = str(assets.get("js") or "")
        if match := _JS_ID_RE.search(js):
            return match.group(1)
    if html:
        # HTML 里可能有多处 /s/player/<id>/ 字样（如 poster 图、缓存预热），
        # 取**出现次数最多**的那个 —— 播放器 JS 会在页面里被引用多次。
        ids = _JS_ID_RE.findall(html)
        if ids:
            return max(set(ids), key=ids.count)
    return ""


def reset_player_cache() -> None:
    """清空播放器 JS 缓存（测试用）。"""
    _player_cache.clear()


async def get_player_js(player_id: str, *, proxy: str | None = None) -> str:
    """取（并缓存）播放器 JS 源码。取不到返回空串（调用方降级）。

    ⚠️ 这是一次**额外请求**，但按 ``player_id`` 缓存 ⇒ 稳态下不随视频数增长
    （同一播放器版本解析任意多条视频只取一次）。
    """
    if not player_id:
        return ""
    cached = _player_cache.get(player_id)
    now = time.time()
    if cached and now - cached[0] < _PLAYER_TTL:
        return cached[1]

    from . import http  # 延迟导入: 与 utils.http 互相独立, 避免形成环

    url = _PLAYER_URL.format(player_id=player_id)
    try:
        async with http.AsyncClient(proxy=proxy, timeout=60) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.warning(f"取播放器 JS 失败: HTTP {response.status_code} player_id={player_id}")
            return ""
        js = response.text
    except Exception as exc:  # noqa: BLE001 - 网络异常都走降级
        logger.warning(f"取播放器 JS 异常: {type(exc).__name__}: {exc}")
        return ""

    if js:
        _player_cache[player_id] = (now, js)
    return js


__all__ = [
    "find_runtime",
    "get_player_js",
    "node_available",
    "player_id_from_response",
    "reset_player_cache",
    "solve_n",
]
