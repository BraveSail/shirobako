"""YouTube SABR 取流 —— 调 ``tools/yt-sabr`` 的 bun 工具（LuanRT/googlevideo 的 SabrStream）。

**为什么是子进程**: SABR 协议（UMP 分帧、自定义 varint、请求序列、SABR_REDIRECT 处理）
已由 ``LuanRT/googlevideo`` 完整实现且实测可用（1080p h264+AAC 全量拉取）。
项目此前自研的 Python 实现与逐字段复刻最终卡在协议细节上（``rn=0`` 首请求、
``SABR_REDIRECT`` 换 URL 等），因此**不再自研** —— 直接用该库, 本模块只负责
「备料 → 起进程 → 收产物」。

工具目录: ``<repo>/tools/yt-sabr``（``sabr.mjs`` + ``package.json``, 依赖仅 ``googlevideo``）。
可用 ``PARSEHUB_YT_SABR_DIR`` 覆盖（测试）。

对外只有一个入口 :func:`fetch_sabr_streams` —— 把一条视频的
``serverAbrStreamingUrl``（**n 已解**）等素材交给工具, 拿回本地 ``video`` / ``audio`` 文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

__all__ = [
    "SABR_TOOL_DIR",
    "SabrFetchError",
    "SabrResult",
    "fetch_sabr_streams",
    "sabr_available",
]


class SabrFetchError(RuntimeError):
    """SABR 取流失败（工具缺失 / 子进程失败 / 产物为空）。"""


def _default_tool_dir() -> Path:
    # src/parsehub/provider_api/youtube_sabr.py -> 仓库根 /tools/yt-sabr
    return Path(__file__).resolve().parents[4] / "tools" / "yt-sabr"


SABR_TOOL_DIR = Path(os.environ.get("PARSEHUB_YT_SABR_DIR") or _default_tool_dir()).resolve()
"""工具目录（``sabr.mjs`` 所在处）—— 绝对路径（env 覆盖也 resolve）。"""

_SCRIPT_NAME = "sabr.mjs"

#: 单次取流的总超时（秒）—— 45MB 级视频 + 重试余量。慢网络下可放宽。
DEFAULT_TIMEOUT = 900.0


def sabr_available() -> bool:
    """能否用 SABR 取流: 有 bun 运行时, 且工具脚本存在。"""
    if not shutil.which("bun"):
        return False
    return (SABR_TOOL_DIR / _SCRIPT_NAME).is_file()


def _runtime() -> str:
    runtime = shutil.which("bun")
    if not runtime:
        raise SabrFetchError("找不到 bun 运行时（SABR 取流需要它）")
    return runtime


@dataclass(frozen=True, slots=True)
class SabrResult:
    """一次 SABR 取流的结果（本地文件路径 + 实际选中的 itag / 字节数）。

    ``video_path`` / ``audio_path`` 的扩展名由**实际选中的编码**决定：
    av1/avc → ``.mp4``/``.m4a``；vp9 → ``.webm``/``.webm``。
    调用方据此定最终输出的容器（**别写死 mp4**）。
    """

    video_path: Path
    audio_path: Path
    video_itag: int
    audio_itag: int
    video_bytes: int
    audio_bytes: int


def _build_input_payload(  # noqa: PLR0913 - 素材字段本来就多, 平铺更直白
    video_id: str,
    *,
    abr_url: str,
    ustreamer_config: str,
    adaptive_formats: list[dict],
    cookie: str,
    video_quality: str,
    codec_priority: tuple[str, ...] = (),
    video_itag: int = 0,
    audio_itag: int = 0,
    user_agent: str = "",
) -> dict:
    payload = {
        "videoId": video_id,
        "serverAbrStreamingUrl": abr_url,
        "videoPlaybackUstreamerConfig": ustreamer_config,
        "adaptiveFormats": adaptive_formats,
        "cookie": cookie,
        "videoQuality": video_quality,
    }
    if codec_priority:
        payload["codecPriority"] = list(codec_priority)
    if video_itag:
        payload["videoItag"] = video_itag
    if audio_itag:
        payload["audioItag"] = audio_itag
    if user_agent:
        payload["userAgent"] = user_agent
    return payload


async def fetch_sabr_streams(  # noqa: PLR0913 - 素材字段本来就多, 平铺更直白
    video_id: str,
    *,
    abr_url: str,
    ustreamer_config: str,
    adaptive_formats: list[dict],
    cookie: str,
    output_dir: Path,
    video_quality: str = "1080p",
    codec_priority: tuple[str, ...] = (),
    video_itag: int = 0,
    audio_itag: int = 0,
    user_agent: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    progress_callback=None,
    progress_total: int = 0,
) -> SabrResult:
    """跑 SABR 工具把视频/音频拉到 ``output_dir``。

    产物名按**实际选中的容器**定：``video.mp4``+``audio.m4a``（av1/avc）
    或 ``video.webm``+``audio.webm``（vp9）—— 见 :class:`SabrResult`。

    失败抛 :class:`SabrFetchError`（调用方据此降级或明示错误, **不静默**）。

    :param codec_priority: 编码优先级（如 ``("av1","vp9","avc")``）；空 = 工具默认。
    :param video_itag: 显式视频 itag（0 = 不指定）；优先级高于 ``codec_priority``。
    :param audio_itag: 显式音频 itag（0 = 不指定）。
    :param user_agent: 请求 UA（**必须与取流/无头刷新一致** —— 见 skill 的 UA 单一真源）。
        空 = 用工具内置默认（两者当前是同一个 Chrome/146 串）。
    :param progress_callback: 可选的 ``async (current, total)`` —— 按临时文件大小
        轮询回推进度; ``progress_total`` 是预期的总字节（0 = 不报进度）。
    """
    script = SABR_TOOL_DIR / _SCRIPT_NAME
    if not script.is_file():
        raise SabrFetchError(f"SABR 工具不存在: {script}")

    runtime = _runtime()
    # ⚠️ **必须绝对化** —— bun 子进程的 cwd 是工具目录（不是本进程的 cwd），
    # 相对路径会被它按**自己的** cwd 解析，于是它找不到 Python 写好的输入文件：
    # 生产实际踩过（bs.download_dir = "downloads" 是相对路径 ⇒ ENOENT）。
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / "_sabr_input.json"
    input_path.write_text(
        json.dumps(
            _build_input_payload(
                video_id,
                abr_url=abr_url,
                ustreamer_config=ustreamer_config,
                adaptive_formats=adaptive_formats,
                cookie=cookie,
                video_quality=video_quality,
                codec_priority=codec_priority,
                video_itag=video_itag,
                audio_itag=audio_itag,
                user_agent=user_agent,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    logger.info(f"SABR 取流: {video_id} (itag 素材 {len(adaptive_formats)} 条, 产物目录 {output_dir})")
    proc = await asyncio.create_subprocess_exec(  # pylint: disable=no-member  # pylint 解析不到 asyncio.subprocess
        runtime,
        str(script),
        "--input",
        str(input_path),
        "--outdir",
        str(output_dir),
        cwd=str(SABR_TOOL_DIR),
        stdout=asyncio.subprocess.PIPE,  # pylint: disable=no-member
        stderr=asyncio.subprocess.PIPE,  # pylint: disable=no-member
    )

    async def _watch_progress() -> None:
        """轮询产物大小回报进度（子进程结束时由外层取消）。"""
        if not progress_callback or progress_total <= 0:
            return
        try:
            while True:
                await asyncio.sleep(2.0)
                current = 0
                for name in ("video.mp4", "audio.m4a"):
                    candidate = output_dir / name
                    if candidate.exists():
                        current += candidate.stat().st_size
                await progress_callback(current, progress_total)
        except asyncio.CancelledError:
            return

    watcher = asyncio.create_task(_watch_progress())
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise SabrFetchError(f"SABR 取流超时（{timeout:.0f}s）") from exc
    finally:
        watcher.cancel()

    stderr_text = (stderr or b"").decode("utf-8", "replace").strip()
    stdout_text = (stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        # ⚠️ 报错必须带上**工具自己的 stdout/stderr** —— 只报「退出码 N」会让现场
        # 无法定位（工具把错误写成一行 JSON 到 stdout；node/bun 的报错在 stderr）。
        detail = stdout_text[-500:] or stderr_text[-500:] or f"退出码 {proc.returncode}"
        raise SabrFetchError(f"SABR 工具失败(退出码 {proc.returncode}): {detail}")

    payload: dict = {}
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
    if not payload.get("ok"):
        raise SabrFetchError(f"SABR 工具无有效输出: {stdout_text[-300:] or stderr_text[-300:]}")

    result = SabrResult(
        video_path=Path(payload["videoPath"]),
        audio_path=Path(payload["audioPath"]),
        video_itag=int(payload.get("videoItag") or 0),
        audio_itag=int(payload.get("audioItag") or 0),
        video_bytes=int(payload.get("videoBytes") or 0),
        audio_bytes=int(payload.get("audioBytes") or 0),
    )
    if result.video_bytes <= 0 or result.audio_bytes <= 0:
        raise SabrFetchError("SABR 产物为空（video/audio 字节为 0）")

    if progress_callback and progress_total > 0:
        await progress_callback(result.video_bytes + result.audio_bytes, progress_total)

    logger.info(
        f"SABR 取流完成: itag {result.video_itag}+{result.audio_itag}, "
        f"{result.video_bytes / 1048576:.1f}MB + {result.audio_bytes / 1048576:.1f}MB"
    )
    return result
