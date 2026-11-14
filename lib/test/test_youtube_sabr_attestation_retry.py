"""SABR：attestation 被拒时要清掉该视频的 PO token 缓存并重新生成再试一次。

## 真实故障（2026-11-14，用户报 YouTube 直播链接）

```
下载错误: SABR 取流失败: SABR 工具失败(退出码 1): {"ok":false,"error":"Stream protection attestation rejected after 10 attempts"}
```

诊断结果（全部实测）：

| 项 | 值 |
| --- | --- |
| 当前 cookie | 4136 字符，30 项，`SAPISID`/`SID`/`HSID`/`SSID` 齐全，指纹 `bef6b5ed` |
| token 缓存 | `YV2mEs9dr2A.bef6b5ed.json` 存在，**指纹与当前身份一致**、`expiresAt` 在两天后 |
| 对照视频 | `UoVIgIpRr-U`（同 cookie / 同 IP / 同工具）→ **下载成功** |
| 删缓存重试 | **下载成功**：1080p h264 + aac，3.4MB / 15s |

⇒ 缓存里的 token **身份对、没过期，但服务端已经不认它**。而 `sabr.mjs` 只要
「文件存在 + expiresAt 未过」就**无条件复用**，被拒 10 次后直接放弃，既不删也不重生成。
这条兜底放在 Python 侧：识别 attestation 失败 → 删该视频的缓存 → 重跑一次工具。

## 契约

- 只删 **`<videoId>.<sha256(cookie)[:8]>.json`** 这一份，不碰目录里其它视频的 token；
- 只在**真的是 attestation 失败**时触发，别的失败（超时、无流）不重试；
- 只重试一次，第二次还失败就如实报错（不无限循环）。
"""

from __future__ import annotations

import hashlib

import pytest

from parsehub.provider_api import youtube_sabr as ys


class TestAttestationDetection:
    def test_detects_rejected(self) -> None:
        assert ys._is_attestation_rejected(
            '{"ok":false,"error":"Stream protection attestation rejected after 10 attempts"}', ""
        )

    def test_detects_required(self) -> None:
        assert ys._is_attestation_rejected(
            '{"ok":false,"error":"Cannot proceed with stream: attestation required"}', ""
        )

    def test_detects_in_stderr(self) -> None:
        assert ys._is_attestation_rejected("", "[sabr] stream error: attestation rejected")

    def test_ignores_other_failures(self) -> None:
        assert not ys._is_attestation_rejected('{"ok":false,"error":"sabr.malformed_config"}', "")
        assert not ys._is_attestation_rejected("", "timeout")
        assert not ys._is_attestation_rejected("", "")


class TestClearPotCache:
    def test_removes_only_this_video_and_identity(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ys, "_POT_CACHE_DIR", tmp_path)
        cookie = "SID=abc; SAPISID=def"
        tag = hashlib.sha256(cookie.encode()).hexdigest()[:8]

        (tmp_path / f"VID1.{tag}.json").write_text("{}")
        keep_video = tmp_path / f"VID2.{tag}.json"
        keep_video.write_text("{}")
        keep_identity = tmp_path / "VID1.aaaaaaaa.json"
        keep_identity.write_text("{}")

        removed = ys._clear_pot_cache("VID1", cookie)

        assert removed == f"VID1.{tag}.json"
        assert not (tmp_path / f"VID1.{tag}.json").exists()
        assert keep_video.exists(), "别的视频的 token 不能删"
        assert keep_identity.exists(), "别的身份的 token 不能删"

    def test_missing_file_is_not_an_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ys, "_POT_CACHE_DIR", tmp_path)
        assert ys._clear_pot_cache("NOPE", "SID=x") == ""

    def test_empty_video_id_is_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ys, "_POT_CACHE_DIR", tmp_path)
        assert ys._clear_pot_cache("", "SID=x") == ""

    def test_anonymous_tag(self, tmp_path, monkeypatch) -> None:
        """cookie 为空时 tag 是 ``anon``（与 sabr.mjs 的 ``'anon'`` 分支一致）。"""
        monkeypatch.setattr(ys, "_POT_CACHE_DIR", tmp_path)
        (tmp_path / "VID1.anon.json").write_text("{}")
        assert ys._clear_pot_cache("VID1", "") == "VID1.anon.json"
