"""重试循环的**结构化验证**（不依赖服务端是否拒绝假 token）。

前几轮真机验证反复失败的原因：植入的假 token 有时被服务端接受、有时被拒，
没有稳定触发条件。所以这里直接对重试逻辑做受控测试 ——
mock 掉 `_run_tool` 的返回值序列，断言：
1. attestation 失败时确实清缓存并重跑；
2. 最多跑 `_MAX_TOKEN_REGENERATIONS` 次；
3. 非 attestation 失败不重试；
4. 重试成功时返回成功结果。

## 为什么必须在 bot 侧（而不是 lib/test）跑

`fetch_sabr_streams` 内部的 `_run_tool` 是闭包，lib 的 pytest 环境没装 pyrogram
依赖链；这里用 monkeypatch 直接替换 `_clear_pot_cache` 并构造一个假的可执行文件
来驱动 —— 但要真正控制 `_run_tool`，只能 patch 模块级的 subprocess 创建。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from parsehub.provider_api import youtube_sabr as ys


def _payload_json(video: Path, audio: Path) -> str:
    return json.dumps(
        {
            "ok": True,
            "videoPath": str(video),
            "audioPath": str(audio),
            "videoItag": 399,
            "audioItag": 140,
            "videoBytes": 100,
            "audioBytes": 50,
        }
    )


class _FakeProc:
    """假的 asyncio subprocess：按预设序列返回 (returncode, stdout, stderr)。"""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


async def _run_fetch(tmp_path: Path, responses: list[tuple[int, str]], *, cookie: str = "SID=x") -> tuple[list[str], object]:
    """跑一次 fetch_sabr_streams，返回 (被删的缓存文件名, 结果/异常)。"""
    cleared: list[str] = []
    seq = list(responses)

    async def fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not seq:
            raise AssertionError("subprocess 被调用次数超出预期序列")
        code, out = seq.pop(0)
        return _FakeProc(code, out.encode())

    def fake_clear(video_id: str, cookie_str: str) -> str:
        tag = hashlib.sha256(cookie_str.encode()).hexdigest()[:8] if cookie_str else "anon"
        name = f"{video_id}.{tag}.json"
        cleared.append(name)
        return name

    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    video.write_bytes(b"v" * 10)
    audio.write_bytes(b"a" * 10)

    with (
        patch.object(ys, "_clear_pot_cache", side_effect=fake_clear),
        # 测试机（161 宿主）没有 bun —— 打桩 `_runtime` 免得在入口就抛「找不到 bun」，
        # 那样根本走不到重试逻辑。工具脚本存在性也一样打桩。
        patch.object(ys, "_runtime", return_value="/usr/bin/fake-bun"),
        patch.object(ys, "SABR_TOOL_DIR", tmp_path),
        patch.object(ys.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec),
    ):
        (tmp_path / "sabr.mjs").write_text("// stub", encoding="utf-8")
        try:
            result = await ys.fetch_sabr_streams(
                "VID",
                abr_url="https://example.com/abr",
                ustreamer_config="cfg",
                adaptive_formats=[{"itag": 399}],
                cookie=cookie,
                output_dir=tmp_path,
            )
            return cleared, result
        except Exception as exc:  # noqa: BLE001
            return cleared, exc


class TestAttestationRetry:
    def test_retries_after_attestation_rejection(self, tmp_path) -> None:
        """第一次被拒 → 清缓存 → 第二次成功。"""
        ok = _payload_json(tmp_path / "video.mp4", tmp_path / "audio.m4a")
        cleared, result = asyncio.run(
            _run_fetch(
                tmp_path,
                [
                    (1, '{"ok":false,"error":"Stream protection attestation rejected after 10 attempts"}'),
                    (0, ok),
                ],
            )
        )
        assert len(cleared) == 1, f"应该清一次缓存，实际: {cleared}"
        assert not isinstance(result, Exception), f"应该成功，实际: {result}"

    def test_capped_at_max_regenerations(self, tmp_path) -> None:
        """一直失败时，最多重跑到 _MAX_TOKEN_REGENERATIONS 次就放弃。"""
        reject = '{"ok":false,"error":"attestation rejected after 10 attempts"}'
        responses = [(1, reject)] * (ys._MAX_TOKEN_REGENERATIONS + 2)
        cleared, result = asyncio.run(_run_fetch(tmp_path, responses))
        assert len(cleared) == ys._MAX_TOKEN_REGENERATIONS, (
            f"清缓存次数应等于上限 {ys._MAX_TOKEN_REGENERATIONS}，实际 {len(cleared)}"
        )
        assert isinstance(result, Exception), "最终应该失败"

    def test_no_retry_for_other_failures(self, tmp_path) -> None:
        """非 attestation 失败（如 malformed_config）不重试、不清缓存。"""
        cleared, result = asyncio.run(
            _run_fetch(tmp_path, [(1, '{"ok":false,"error":"sabr.malformed_config"}')])
        )
        assert cleared == [], f"不该清缓存，实际: {cleared}"
        assert isinstance(result, Exception)

    def test_success_first_try_does_not_clear(self, tmp_path) -> None:
        ok = _payload_json(tmp_path / "video.mp4", tmp_path / "audio.m4a")
        cleared, result = asyncio.run(_run_fetch(tmp_path, [(0, ok)]))
        assert cleared == []
        assert not isinstance(result, Exception)

    def test_attestation_required_also_retries(self, tmp_path) -> None:
        """``attestation required``（没带 token）走同一条重试路径。"""
        ok = _payload_json(tmp_path / "video.mp4", tmp_path / "audio.m4a")
        cleared, result = asyncio.run(
            _run_fetch(
                tmp_path,
                [
                    (1, '{"ok":false,"error":"Cannot proceed with stream: attestation required"}'),
                    (0, ok),
                ],
            )
        )
        assert len(cleared) == 1
        assert not isinstance(result, Exception)
