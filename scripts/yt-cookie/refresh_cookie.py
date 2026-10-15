#!/usr/bin/env python3
"""无头刷新 YouTube cookie 并固化到本地（161 宿主机侧工具，不进容器）。

**设计要点**

- 浏览器**带登录态**加载真实页面（youtube.com），让 Google 自己完成会话续期 ——
  ``__Secure-*SIDTS`` 这类滚动令牌**只在页面请求里更新**，纯 HTTP 请求拿不到新值。
- 导出浏览器里的全部 cookie（含新签发的），**连同真实 ``expires`` 一起落盘**。
- 全程凭据只落文件（600），**不打印值到 stdout**。

**关于有效期**：本脚本**不假设** cookie 寿命。第一次运行会把浏览器给出的 ``expires``
记进 ``cookies.json``（含 ``expires`` 字段）；之后每次刷新都用真实值对比，
从而回答「还剩多久到期」「要不要人工介入」。

用法（在 161 上）::

    /opt/yt-browser/bin/python /opt/yt-browser/refresh_cookie.py
    /opt/yt-browser/bin/python /opt/yt-browser/refresh_cookie.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

CK_DIR = Path("/root/yt-cookie")
CK_JSON = CK_DIR / "cookies.json"
CK_NS = CK_DIR / "cookies.txt"
STAMP = CK_DIR / "refreshed_at.txt"
LOG = CK_DIR / "refresh.log"

#: 登录态的标志性 cookie —— 少了它说明这份 cookie 根本不是登录态
LOGIN_MARKERS = ("SID", "HSID", "SSID", "__Secure-1PSID")

#: curl 按域匹配；两套域都写（YouTube 与 Google 账号体系互相依赖）
DOMAIN_SETS = (".youtube.com", ".google.com")


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cookies() -> dict[str, str]:
    """读种子 cookie。兼容两种形态（早期只存 ``{name: value}``）。"""
    data = json.loads(CK_JSON.read_text(encoding="utf-8"))
    # 带 expires 的新形态：{"cookies": {"name": {...}}, "captured_at": ...}
    if isinstance(data, dict) and "cookies" in data:
        return {k: v["value"] for k, v in data["cookies"].items()}
    return data


def load_expiry() -> dict[str, int]:
    """读上次记录的真实过期时间（``name -> unix ts``；无则空）。"""
    try:
        data = json.loads(CK_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件不该拦住刷新
        return {}
    if isinstance(data, dict) and "cookies" in data:
        return {k: int(v.get("expires") or -1) for k, v in data["cookies"].items()}
    return {}


def write_netscape(cookies: dict[str, str], expires: dict[str, int], path: Path) -> None:
    """写 Netscape cookie 文件。

    ⚠️ **过期时间用浏览器给的真实值**（``expires``）；拿不到时用会话级 ``0``
    —— 不要用一个自己编的"180 天后"去顶替真实值，那会让排查时看不到真实寿命。
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# 由 refresh_cookie.py 生成; 含登录态, 权限 600",
        "# expiry 列取自浏览器给出的真实 expires（拿不到则为 0 = 会话级）",
        "",
    ]
    for k, v in cookies.items():
        exp = expires.get(k, 0)
        if not exp or exp < 0:
            exp = 0
        for domain in DOMAIN_SETS:
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t{exp}\t{k}\t{v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def sapisidhash(sapisid: str, origin: str = "https://www.youtube.com") -> str:
    """Google 的 SAPISIDHASH 授权头算法（SHA1(时间戳_origin_值)，前置时间戳）。"""
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts}_{sapisid}_{origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def expiry_report(raw: list[dict]) -> str:
    """把真实 expires 汇总成一句人类可读的结论（只统计登录关键项）。"""
    now = int(time.time())
    notable = []
    for c in raw:
        name, exp = c.get("name", ""), int(c.get("expires") or -1)
        if name in LOGIN_MARKERS or name.startswith("__Secure-"):
            if exp and exp > 0:
                days = (exp - now) / 86400
                notable.append(f"{name}={datetime.fromtimestamp(exp):%Y-%m-%d}({days:.0f}天后)")
            else:
                notable.append(f"{name}=会话级")
    return "; ".join(notable) if notable else "（无关键项）"


async def refresh(*, headless: bool = True) -> dict[str, str]:
    seed = load_cookies()
    missing = [k for k in LOGIN_MARKERS if k not in seed]
    if missing:
        raise SystemExit(f"传入的 cookie 缺少登录标志 {missing} —— 不是登录态，拒绝继续")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(locale="en-US")
        # ⚠️ 两个域都要注: 长期登录项 (SID/HSID/SSID/SAPISID) 属于 **.google.com**,
        # YouTube 自己的滚动令牌属于 **.youtube.com**。只注 .youtube.com 的话
        # 页面看起来"已登录"(靠 __Secure-3* 就够渲染头像), 但 Google 不认这套身份,
        # **SID 等长期项不会被续期、刷新后直接从浏览器里消失** ——
        # 实测后果: 导出从 24 条掉到 15 条, 只剩 __Secure-3*/GPS/PREF, 登录态实际已丢。
        # 这是真实踩过的坑（2026-10-16）, 分布写死不要改。
        # ``__Secure-`` 前缀必须 ``secure=True``, 否则 Chromium 直接拒收
        # (报 "Invalid cookie fields")。
        await ctx.add_cookies(
            [
                {
                    "name": k,
                    "value": v,
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
                for k, v in seed.items()
                for domain in DOMAIN_SETS
            ]
        )
        page = await ctx.new_page()
        log("打开 youtube.com（带登录态）…")
        await page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=90_000)
        # 让页面把会话 cookie 滚动一次（SIDTS 类值只在这里更新）
        await page.wait_for_timeout(4000)

        logged_in = await page.evaluate(
            """() => {
                const sel = ['#avatar-btn', 'ytd-topbar-menu-button-renderer', 'button[aria-label*="Account"]'];
                return sel.some(s => document.querySelector(s) !== null);
            }"""
        )
        log(f"登录态断言(页面元素): {logged_in}")

        # 触发一次真正的已登录请求，促使服务端续期
        try:
            await page.goto("https://www.youtube.com/feed/history", wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3000)
        except Exception as e:  # noqa: BLE001
            log(f"history 页加载异常(不致命): {type(e).__name__}")

        # ★ 保留浏览器给出的完整 cookie 对象（含 expires）—— 别只取 name/value
        fresh_raw = await ctx.cookies()
        await browser.close()

    out = {c["name"]: c["value"] for c in fresh_raw}
    expires = {c["name"]: int(c.get("expires") or -1) for c in fresh_raw}
    log(f"导出 cookie: {len(out)} 条（原 {len(seed)} 条）")
    gained = sorted(set(out) - set(seed))
    if gained:
        log(f"新增/变化项: {gained}")
    log(f"真实有效期: {expiry_report(fresh_raw)}")

    payload = {
        "captured_at": int(time.time()),
        "cookies": {k: {"value": v, "expires": expires.get(k, -1)} for k, v in out.items()},
    }
    CK_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    CK_JSON.chmod(0o600)
    write_netscape(out, expires, CK_NS)
    STAMP.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")

    if "SAPISID" in out:
        # 只打印是否可算，不打印值
        _ = sapisidhash(out["SAPISID"])
        log("SAPISID 存在，可生成 SAPISIDHASH 授权头")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验现有 cookie 与上次刷新时间")
    args = ap.parse_args()

    CK_DIR.mkdir(exist_ok=True)
    if args.check:
        ck = load_cookies()
        exp = load_expiry()
        now = int(time.time())
        print(f"cookie 条目: {len(ck)}")
        print(f"登录标志: {[k for k in LOGIN_MARKERS if k in ck]}")
        print(f"上次刷新: {STAMP.read_text().strip() if STAMP.exists() else '从未'}")
        # ★ 回答"还有多久到期"就是这一行
        soon = [
            f"{k}={datetime.fromtimestamp(v):%Y-%m-%d}({(v - now) / 86400:.0f}天后)"
            for k, v in exp.items()
            if v and v > 0 and k in (*LOGIN_MARKERS, "__Secure-1PSIDTS", "__Secure-3PSIDTS")
        ]
        print("关键项到期: " + ("; ".join(soon) if soon else "（未记录 —— 下次刷新会补上）"))
        return 0

    import asyncio

    try:
        asyncio.run(refresh())
    except Exception as e:  # noqa: BLE001
        log(f"刷新失败: {type(e).__name__}: {str(e)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
