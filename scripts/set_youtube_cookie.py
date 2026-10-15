#!/usr/bin/env python3
"""把 YouTube cookie 写进 ``platform_config.yaml``（供无头刷新脚本调用）。

**为什么需要它**: 登录态 cookie 会过期，靠宿主机上的
``/opt/yt-browser/refresh_cookie.py``（Playwright + Chromium）滚动续期。
续期后必须把新值写进 bot 的配置 —— 本脚本就是那一步，并且:

- **原子写**: 先写同目录临时文件再 ``os.replace`` —— 正在跑的 bot 永远读不到半个文件
  （热重载是按 mtime 触发的，读到半截会当成坏配置）。
- **回读校验**: 写完立刻读回来比对，不一致就回滚备份。
- **备份**: 每次改都留一份带时间戳的副本（配置里还有别的平台的凭据）。
- **不开代理**: 按用户要求 youtube 走**直连**（``disable_parser_proxy`` /
  ``disable_downloader_proxy``），只留 cookie。

用法（在 161 的仓库根目录）::

    python3 scripts/set_youtube_cookie.py <cookie文件>
    python3 scripts/set_youtube_cookie.py --from-json /root/yt-cookie/cookies.json
    python3 scripts/set_youtube_cookie.py --check          # 只看现状, 不改

``<cookie文件>`` 内容为浏览器里复制的 ``k=v; k=v; ...`` 单行串。
``--from-json`` 接受 ``{"name": "value", ...}``（无头刷新脚本导出的形态）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

CONFIG = Path("data/config/platform_config.yaml")

#: 登录态的标志性 cookie —— 少了它这份 cookie 根本不是登录态（写进去也白搭）
LOGIN_MARKERS = ("SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID")


def parse_cookie_text(text: str) -> dict[str, str]:
    """把 ``k=v; k=v`` 串解析成 dict（``Cookie:`` 前缀容忍）。"""
    s = text.strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    out: dict[str, str] = {}
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        key, _, value = seg.partition("=")
        out[key.strip()] = value.strip()
    return out


def load_cookie_file(path: Path) -> dict[str, str]:
    """从 ``.txt``(k=v 串) 或 ``.json``(对象) 读 cookie。"""
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix.lower() == ".json" or text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("cookie JSON 必须是对象 {name: value}")
        # 兼容刷新脚本的新形态: {"captured_at": ..., "cookies": {"name": {"value":…, "expires":…}}}
        # —— 那种形态下 cookie 值在嵌套的 "value" 里, 直接取会拿到 dict。
        if "cookies" in data and isinstance(data["cookies"], dict):
            nested = data["cookies"]
            out: dict[str, str] = {}
            for k, v in nested.items():
                if isinstance(v, dict):
                    out[str(k).strip()] = "" if v.get("value") is None else str(v["value"]).strip()
                else:
                    out[str(k).strip()] = "" if v is None else str(v).strip()
            return out
        return {str(k).strip(): "" if v is None else str(v).strip() for k, v in data.items()}
    return parse_cookie_text(text)


def as_cookie_string(cookies: dict[str, str]) -> str:
    """dict -> 配置里存的单行 ``k=v; k=v`` 串（与其它平台同一形态）。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _write_atomically(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace`` —— 读者要么看到旧内容, 要么看到新的。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)  # 配置里有全平台凭据, 别放宽权限
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("cookie_file", nargs="?", type=Path, help="cookie 文件 (k=v 串或 JSON)")
    src.add_argument("--from-json", type=Path, metavar="JSON", help="从 JSON 对象读 cookie")
    ap.add_argument("--check", action="store_true", help="只打印现状, 不修改")
    ap.add_argument(
        "--keep-proxy",
        action="store_true",
        help="保留 youtube 的代理配置（默认按需求关掉, 走直连）",
    )
    args = ap.parse_args()

    if not CONFIG.exists():
        print(f"找不到配置: {CONFIG}（请在仓库根目录运行）")
        return 2

    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    platforms = data.setdefault("platforms", {})
    entry = platforms.setdefault("youtube", {}) or {}

    if args.check:
        cookies = entry.get("cookies") or []
        names = set()
        if cookies:
            names = set(parse_cookie_text(str(cookies[0])))
        print(f"youtube.cookies: {len(cookies)} 条, 字段 {len(names)} 个")
        print(f"  登录标志: {[m for m in LOGIN_MARKERS if m in names]}")
        print(f"  parser_proxy 关闭: {bool(entry.get('disable_parser_proxy'))}")
        print(f"  parser_proxies: {entry.get('parser_proxies')}")
        return 0

    path = args.from_json or args.cookie_file
    if path is None:
        print("需要给出 cookie 文件（或用 --check 只看现状）")
        return 2
    if not path.exists():
        print(f"cookie 文件不存在: {path}")
        return 2

    cookies = load_cookie_file(path)
    if not cookies:
        print("cookie 解析出来是空的")
        return 2

    missing = [m for m in LOGIN_MARKERS if m not in cookies]
    if missing:
        # 不阻断: 也许 Google 改了字段名 —— 但必须让调用方看见
        print(f"⚠️  缺少登录标志 {missing} —— 这份 cookie 可能不是登录态", file=sys.stderr)
    print(f"cookie 字段 {len(cookies)} 个: {sorted(cookies)}")

    entry["cookies"] = [as_cookie_string(cookies)]
    if not args.keep_proxy:
        # 按用户要求: youtube 走直连（代理已关）。留着 disable_* 让行为显式,
        # 不靠"配置里没有 proxies"这种隐式状态。
        entry["disable_parser_proxy"] = True
        entry["disable_downloader_proxy"] = True
        entry.pop("parser_proxies", None)
        entry.pop("downloader_proxies", None)
    platforms["youtube"] = entry

    backup = CONFIG.with_name(f"{CONFIG.name}.bak-yt-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(CONFIG, backup)
    print(f"已备份: {backup.name}")

    _write_atomically(CONFIG, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    # 回读校验 —— 不通过就回滚（不能留下半坏的配置）
    check = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    got = ((check.get("platforms") or {}).get("youtube") or {}).get("cookies") or []
    expected = as_cookie_string(cookies)
    if len(got) != 1 or got[0] != expected:
        shutil.copy2(backup, CONFIG)
        print("✗ 回读校验失败, 已回滚", file=sys.stderr)
        return 1
    print("✓ 写入并回读校验通过（bot 会在下次解析时热重载）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
