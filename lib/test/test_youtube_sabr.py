"""SABR 素材提取与接线的离线测试。

被测点（都是纯函数 / 无网络, 用假子进程）:

1. ``extract_sabr_source``: 判据是「adaptiveFormats 没有明文 url」——
   有明文 url 的视频**不得**返回 SABR 素材（否则会绕开直链下载器, 白跑一次子进程）。
2. ``select_streams``: SABR 视频回报 SABR 素材里的画质（而非 360p 合一档）。
3. ``fetch_sabr_streams``: 子进程协议 —— 正常 JSON 输出 → ``SabrResult``;
   工具缺失 / 非零退出 / 空产物 → ``SabrFetchError``（不静默）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parsehub.provider_api.youtube_sabr import (  # noqa: E402
    SabrFetchError,
    fetch_sabr_streams,
    sabr_available,
)
from parsehub.provider_api.youtube_video import (  # noqa: E402
    SabrSource,
    YoutubeVideo,
    extract_sabr_source,
    select_streams,
)

# ── 夹具 ─────────────────────────────────────────────────────────────────


def _player_response(*, with_abras: bool = True, adaptive_has_url: bool = False) -> dict:
    adaptive = [
        {
            "itag": 137,
            "mimeType": 'video/mp4; codecs="avc1.640028"',
            "width": 1920,
            "height": 1080,
            "bitrate": 4_000_000,
            "contentLength": "44000000",
        },
        {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 128_000, "contentLength": "1500000"},
    ]
    if adaptive_has_url:
        adaptive[0]["url"] = "https://rr1---sn-x.googlevideo.com/videoplayback?..."
    data: dict = {
        "streamingData": {"adaptiveFormats": adaptive},
        "playerConfig": {
            "mediaCommonConfig": {"mediaUstreamerRequestConfig": {"videoPlaybackUstreamerConfig": "A" * 64}}
        },
    }
    if with_abras:
        data["streamingData"]["serverAbrStreamingUrl"] = "https://rr1---sn-x.googlevideo.com/videoplayback?sabr=1&n=abc"
    return data


def _video(sabr: SabrSource | None = None) -> YoutubeVideo:
    return YoutubeVideo(video_id="Nbc7I6y97FI", title="t", duration=90, sabr=sabr)


def _sabr_source() -> SabrSource:
    return SabrSource(
        abr_url="https://x.googlevideo.com/videoplayback?sabr=1",
        ustreamer_config="A" * 64,
        adaptive_formats=tuple(_player_response()["streamingData"]["adaptiveFormats"]),
    )


# ── extract_sabr_source ──────────────────────────────────────────────────


def test_extract_returns_none_without_abr_url() -> None:
    """没有 serverAbrStreamingUrl（老视频形态）→ 不该有 SABR 素材。"""
    assert extract_sabr_source(_player_response(with_abras=False)) is None


def test_extract_returns_none_when_adaptive_has_url() -> None:
    """adaptiveFormats 里有明文 url ⇒ 用得上直链, 不该走 SABR。"""
    assert extract_sabr_source(_player_response(adaptive_has_url=True)) is None


def test_extract_returns_source_for_sabr_only_video() -> None:
    """无明文 url + 有 abr url + 有 ustreamerConfig ⇒ 返回素材。"""
    source = extract_sabr_source(_player_response())
    assert source is not None
    assert source.abr_url.startswith("https://")
    assert source.ustreamer_config
    assert len(source.adaptive_formats) == 2


def test_extract_none_without_ustreamer_config() -> None:
    """缺 ustreamerConfig 也不能走 SABR（工具没法建请求）。"""
    data = _player_response()
    data["playerConfig"] = {}
    assert extract_sabr_source(data) is None


def test_extract_none_on_empty_or_broken_payload() -> None:
    for data in ({}, {"streamingData": None}, {"streamingData": {"adaptiveFormats": []}}):
        assert extract_sabr_source(data) is None


# ── select_streams（SABR 分支）─────────────────────────────────────────────


def test_select_streams_returns_sabr_quality_not_360p() -> None:
    """SABR 视频: 画质回报应是素材里的最高档（1080p）, 不是 360p 合一档。

    旧行为: 只有 itag=18（360p）明文流 ⇒ select_streams 回 360p,
    下载后用户拿到低画质。新行为: sabr 非空 ⇒ 画质取 SABR 素材的最高视频档。
    """
    # 模拟 watch 页给的 360p 合一档（SABR 视频的真实形态）
    video = _video(sabr=_sabr_source())
    video.streams = []
    selected = select_streams(video, max_height=1080, allow_mux=True)
    assert (selected.width, selected.height) == (1920, 1080)
    assert selected.video_url == ""  # SABR 没有明文直链, 由 sabr 素材承载
    assert selected.audio_url is None


def test_select_streams_sabr_respects_max_height() -> None:
    """max_height=720 时, SABR 画的画质回报不超过 720。"""
    video = _video(sabr=_sabr_source())
    selected = select_streams(video, max_height=720)
    assert selected.height <= 720


# ── fetch_sabr_streams（子进程协议）────────────────────────────────────────


def _fake_tool(tmp_path: Path, body: str, exit_code: int = 0) -> Path:
    """写一个假 ``sabr.mjs`` —— 内容无关紧要, 我们替换的是"运行器"。"""
    tool_dir = tmp_path / "yt-sabr"
    tool_dir.mkdir()
    (tool_dir / "sabr.mjs").write_text(body)
    return tool_dir


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover - 只防未调用
        pass


def _run_fetch(monkeypatch: pytest.MonkeyPatch, tool_dir: Path, proc: _FakeProc, out: Path):
    """跑 fetch_sabr_streams, 但把 ``create_subprocess_exec`` 换成假进程。"""
    import parsehub.provider_api.youtube_sabr as mod

    monkeypatch.setattr(mod, "SABR_TOOL_DIR", tool_dir)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/bun" if name == "bun" else None)

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec)
    return asyncio.run(
        fetch_sabr_streams(
            "Nbc7I6y97FI",
            abr_url="https://x/?sabr=1",
            ustreamer_config="A",
            adaptive_formats=[{"itag": 137}],
            cookie="k=v",
            output_dir=out,
        )
    )


def test_relative_output_dir_is_absolutized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 回归（用户实际踩过）：相对 ``output_dir`` 必须转成绝对路径。

    生产 ``bs.download_dir`` 是相对路径 ``downloads`` —— 子进程的 cwd 是**工具目录**，
    相对路径会被它按自己的 cwd 解析 ⇒ 找不到 Python 写好的输入 JSON（ENOENT）。

    本测试钉住两点：① 传给子进程的 ``--input`` / ``--outdir`` 都是绝对路径；
    ② 输入文件恰好在 ``output_dir.resolve()`` 下（子进程按绝对路径能找到）。
    """
    import parsehub.provider_api.youtube_sabr as mod

    tool_dir = _fake_tool(tmp_path, "// fake")
    seen: dict = {}

    class _Proc:
        returncode = 1
        async def communicate(self):
            return b'{"ok":false,"error":"x"}', b""
        def kill(self):
            pass

    async def _fake_exec(runtime, script, *args, **kwargs):
        seen["argv"] = [runtime, script, *args]
        seen["input"] = args[1]  # --input 的值
        seen["outdir"] = args[3]  # --outdir 的值
        return _Proc()

    monkeypatch.setattr(mod, "SABR_TOOL_DIR", tool_dir)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/bun" if name == "bun" else None)
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SabrFetchError):
        asyncio.run(
            fetch_sabr_streams(
                "x",
                abr_url="u",
                ustreamer_config="c",
                adaptive_formats=[{"itag": 1}],
                cookie="",
                output_dir=Path("downloads") / "视频名" / ".x.sabr",  # 相对路径（生产形态）
            )
        )

    assert Path(seen["input"]).is_absolute(), f"--input 不是绝对路径: {seen['input']}"
    assert Path(seen["outdir"]).is_absolute(), f"--outdir 不是绝对路径: {seen['outdir']}"
    # 输入文件确实写在 outdir 里（子进程按绝对路径能找到）
    assert Path(seen["input"]).parent == Path(seen["outdir"]).resolve()


def test_fetch_sabr_streams_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """工具输出合法 JSON ⇒ SabrResult, 且字段取自输出。"""
    tool_dir = _fake_tool(tmp_path, "// fake")
    out = tmp_path / "out"
    out.mkdir()
    video = out / "video.mp4"
    audio = out / "audio.m4a"
    video.write_bytes(b"v" * 100)
    audio.write_bytes(b"a" * 50)
    payload = {
        "ok": True,
        "videoPath": str(video),
        "audioPath": str(audio),
        "videoItag": 137,
        "audioItag": 140,
        "videoBytes": 100,
        "audioBytes": 50,
    }
    proc = _FakeProc(0, f"[sabr] log line\n{json.dumps(payload)}\n".encode(), b"stderr noise")
    result = _run_fetch(monkeypatch, tool_dir, proc, out)
    assert result.video_itag == 137
    assert result.audio_itag == 140
    assert result.video_bytes == 100
    assert result.audio_path == audio


def test_fetch_sabr_streams_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool_dir = _fake_tool(tmp_path, "// fake")
    out = tmp_path / "out"
    out.mkdir()
    proc = _FakeProc(1, b"", b"boom: no media")
    with pytest.raises(SabrFetchError, match="SABR 工具失败"):
        _run_fetch(monkeypatch, tool_dir, proc, out)


def test_fetch_sabr_streams_empty_product_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool_dir = _fake_tool(tmp_path, "// fake")
    out = tmp_path / "out"
    out.mkdir()
    payload = {
        "ok": True,
        "videoPath": str(out / "video.mp4"),
        "audioPath": str(out / "audio.m4a"),
        "videoItag": 137,
        "audioItag": 140,
        "videoBytes": 0,
        "audioBytes": 0,
    }
    proc = _FakeProc(0, json.dumps(payload).encode(), b"")
    with pytest.raises(SabrFetchError, match="产物为空"):
        _run_fetch(monkeypatch, tool_dir, proc, out)


def test_fetch_missing_tool_raises(tmp_path: Path) -> None:
    import parsehub.provider_api.youtube_sabr as mod

    original = mod.SABR_TOOL_DIR
    mod.SABR_TOOL_DIR = tmp_path / "nope"
    try:
        with pytest.raises(SabrFetchError, match="SABR 工具不存在"):
            asyncio.run(
                fetch_sabr_streams(
                    "x",
                    abr_url="u",
                    ustreamer_config="c",
                    adaptive_formats=[],
                    cookie="",
                    output_dir=tmp_path,
                )
            )
    finally:
        mod.SABR_TOOL_DIR = original


def test_sabr_available_false_without_tool(tmp_path: Path) -> None:
    import parsehub.provider_api.youtube_sabr as mod

    original = mod.SABR_TOOL_DIR
    mod.SABR_TOOL_DIR = tmp_path / "nope"
    try:
        assert sabr_available() is False
    finally:
        mod.SABR_TOOL_DIR = original


def test_build_input_payload_includes_codec_options() -> None:
    """codec_priority / video_itag / audio_itag 要原样进 payload（空值不出现）。"""
    from parsehub.provider_api.youtube_sabr import _build_input_payload

    base = _build_input_payload(
        "vid",
        abr_url="u",
        ustreamer_config="c",
        adaptive_formats=[{"itag": 1}],
        cookie="k=v",
        video_quality="1080p",
    )
    assert "codecPriority" not in base
    assert "videoItag" not in base

    full = _build_input_payload(
        "vid",
        abr_url="u",
        ustreamer_config="c",
        adaptive_formats=[{"itag": 1}],
        cookie="k=v",
        video_quality="1080p",
        codec_priority=("av1", "vp9", "avc"),
        video_itag=399,
        audio_itag=140,
    )
    assert full["codecPriority"] == ["av1", "vp9", "avc"]
    assert full["videoItag"] == 399
    assert full["audioItag"] == 140


def test_sabr_source_carries_codec_options() -> None:
    """SabrSource 的编码字段：默认 av1>vp9>avc（生产默认），显式可覆盖。"""
    from parsehub.provider_api.youtube_video import DEFAULT_SABR_CODEC_PRIORITY

    src = SabrSource(abr_url="u", ustreamer_config="c", adaptive_formats=({"itag": 1},))
    assert src.codec_priority == DEFAULT_SABR_CODEC_PRIORITY == ("av1", "vp9", "avc")
    assert src.video_itag == 0
    assert src.audio_itag == 0

    src2 = SabrSource(
        abr_url="u",
        ustreamer_config="c",
        adaptive_formats=({"itag": 1},),
        codec_priority=("avc",),
        video_itag=399,
        audio_itag=140,
    )
    assert src2.codec_priority == ("avc",)
    assert src2.video_itag == 399


def test_extract_sabr_source_sets_default_codec_priority() -> None:
    """走 extract_sabr_source 造出来的素材要带上默认编码优先级（别漏了这条链路）。"""
    source = extract_sabr_source(_player_response())
    assert source is not None
    assert source.codec_priority == ("av1", "vp9", "avc")
