#!/usr/bin/env python3
"""把刷新后的 cookie 推给 bot（由 refresh-and-push.sh 调用，在 161 宿主机）。

刷新脚本负责"续期"，本脚本负责"接线"：
调用仓库里的 ``scripts/set_youtube_cookie.py`` 原子写入 platform_config.yaml，
再用**容器内**校验确认 bot 看到的是新配置（cookie + 直连）。

失败退非零 —— 让 systemd 的失败状态可见，而不是静默地用旧 cookie。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path("/root/parse_hub_bot")
COOKIE_JSON = Path("/root/yt-cookie/cookies.json")
LOG = Path("/root/yt-cookie/push.log")
CONTAINER = "shirobakobot-bot-1"

#: 登录态判据（**与 refresh_cookie.py 的 `is_login_state` 同源**，也和 yt-dlp 的
#: `_has_auth_cookies` 一致）：``LOGIN_INFO`` 存在 **且** 至少一个 SAPISID 系。
#:
#: ⚠️ 别要求 ``SID``/``HSID``/``SSID``/``SAPISID`` —— 实测有效登录态可能只有
#: ``__Secure-3PAPISID``（那些属于 .google.com，导出时常常不在）。
LOGIN_INFO_COOKIE = "LOGIN_INFO"
SAPISID_COOKIES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")


def is_login_state(cookies: dict[str, str]) -> tuple[bool, str]:
    if not cookies.get(LOGIN_INFO_COOKIE):
        return False, f"缺 {LOGIN_INFO_COOKIE}"
    if not any(cookies.get(k) for k in SAPISID_COOKIES):
        return False, f"缺 {'/'.join(SAPISID_COOKIES)} 中的任一项"
    return True, "ok"


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    if not COOKIE_JSON.exists():
        log(f"没有 cookie 文件: {COOKIE_JSON}")
        return 1

    # 0) 先确认 cookie 是**登录态**再说 —— 只看字段存在不够（失效的 cookie 里 SID 照样在）。
    #    判据与刷新脚本一致: 必须有长期登录项。真正的有效性由 refresh_cookie.py 断言
    #    （ytcfg LOGGED_IN）；这里挡住"被截断/只剩 __Secure-3*"这类明显残缺的输入。
    try:
        data = json.loads(COOKIE_JSON.read_text(encoding="utf-8"))
        values = (
            {k: v.get("value") for k, v in data["cookies"].items()}
            if isinstance(data.get("cookies"), dict)
            else data
        )
    except Exception as e:  # noqa: BLE001
        log(f"cookie 文件读不了: {type(e).__name__}: {e}")
        return 1
    ok, why = is_login_state({k: v for k, v in values.items() if v})
    if not ok:
        # 关键: 不是登录态就别推 —— 推上去会让 bot 用一份无效登录态,
        # 表现成"解析不出来"而没人知道是 cookie 的问题。
        log(f"cookie 不是登录态（{why}）—— 拒绝写入")
        return 1

    # ⚠️ 必须用**仓库的 .venv** —— 系统 python3 没装 yaml。
    repo_py = REPO / ".venv" / "bin" / "python"
    python = str(repo_py) if repo_py.exists() else sys.executable

    # 1) 原子写入 bot 配置（备份 + 回读校验都在那个脚本里）
    r = subprocess.run(
        [python, "scripts/set_youtube_cookie.py", "--from-json", str(COOKIE_JSON)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    for line in (r.stdout or "").splitlines():
        log(f"  set_youtube_cookie: {line}")
    if r.returncode != 0:
        log(f"写入配置失败 (rc={r.returncode}): {(r.stderr or '').strip()[:300]}")
        return 1

    # 2) 真验证: 容器内 import pl_cfg 读一遍（热重载是惰性的，取配置时比对 mtime）。
    #    不发任何网络请求。
    ver = subprocess.run(
        [
            "docker", "exec", "-w", "/app", CONTAINER, "python", "-c",
            "from core import pl_cfg;"
            "c=pl_cfg.roll_cookie('youtube');"
            "print('cookie字段数=', len(c.get_secret_value().split(';')) if c else 0);"
            "print('代理关闭=', pl_cfg.roll_parser_proxy('youtube') is None)",
        ],
        capture_output=True,
        text=True,
    )
    out = (ver.stdout or "").strip()
    if ver.returncode != 0:
        log(f"容器内校验失败: {(ver.stderr or '').strip()[-300:]}")
        return 1
    for line in out.splitlines():
        log(f"  容器内: {line}")
    if "代理关闭= True" not in out and "代理关闭=True" not in out:
        log("⚠️  容器内看到的仍是走代理 —— 配置可能没生效")
        return 1
    log("✓ 容器内已看到新配置（cookie + 直连）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
