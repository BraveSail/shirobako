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

#: ★ **UA 单一真源** —— 与取流侧（``parsehub.provider_api.youtube_video._WEB_USER_AGENT``）
#: 和 SABR 工具（``tools/yt-sabr/sabr.mjs`` 的 ``DEFAULT_UA``）**必须逐字一致**。
#:
#: **取值依据 = curl_cffi 实际发出的 UA**（项目取流主路用它）：``http.IMPERSONATE``
#: 是 ``chrome150`` ⇒ curl_cffi 把 TLS 指纹伪装成 Chrome 150（macOS），UA 必须跟它匹配，
#: 否则两个字段互相矛盾（反爬可以交叉验证）。实测回显：
#: ``https://httpbin.org/user-agent``。
#:
#: 为什么统一: 三个环节（无头刷新 / 取流 / SABR 取流）都在跟同一个账号会话打交道，
#: 同一个 cookie 配三种 UA 会露馅 —— Playwright 默认带的 ``HeadlessChrome/x.y``
#: 标记更是直球暴露自动化（真浏览器 UA 里没有 ``Headless``）。
#:
#: ⚠️ 改这里就要同时改另外两处（三处的值必须完全相同，``test_youtube_ua_consistency.py`` 钉住）:
#:   - ``lib/src/parsehub/provider_api/youtube_video.py`` 的 ``_WEB_USER_AGENT``
#:   - ``tools/yt-sabr/sabr.mjs`` 的 ``DEFAULT_UA``
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

CK_DIR = Path("/root/yt-cookie")
CK_JSON = CK_DIR / "cookies.json"
CK_NS = CK_DIR / "cookies.txt"
STAMP = CK_DIR / "refreshed_at.txt"
LOG = CK_DIR / "refresh.log"

#: 登录态的判据（**与 yt-dlp 的 `_has_auth_cookies` 一致**）：
#: 有 ``LOGIN_INFO`` 且至少有一个 SAPISID 系。
#:
#: ⚠️ **不能要求 ``SID``/``HSID``/``SSID``/``SAPISID``** —— 它们属于 ``.google.com``，
#: 浏览器/导出工具只给 ``.youtube.com`` 那批时就不在里面。实测用户给的一份**有效**
#: 登录态只有 ``__Secure-3PAPISID``（无 ``SID``/``SAPISID``），无头里
#: ``ytcfg LOGGED_IN=True``、取流 31 条 —— 拿旧门槛去拦会把**有效** cookie 判成无效。
LOGIN_INFO_COOKIE = "LOGIN_INFO"
SAPISID_COOKIES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")

#: 展示用：这些是登录相关的关键项（`--check` / 到期报表里列出）
LOGIN_MARKERS = (LOGIN_INFO_COOKIE, *SAPISID_COOKIES)


def is_login_state(cookies: dict[str, str]) -> tuple[bool, str]:
    """判断一份 cookie 是否是登录态，返回 ``(是否, 原因)``。

    只做**静态**判断（字段在不在）。**有效**与否最终只能让平台自己说：
    无头里的 ``ytcfg LOGGED_IN`` / 取流是否 ``OK``。
    """
    if not cookies.get(LOGIN_INFO_COOKIE):
        return False, f"缺 {LOGIN_INFO_COOKIE}"
    if not any(cookies.get(k) for k in SAPISID_COOKIES):
        return False, f"缺 {'/'.join(SAPISID_COOKIES)} 中的任一项"
    return True, "ok"

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
    ok, why = is_login_state(seed)
    if not ok:
        raise SystemExit(f"传入的 cookie 不是登录态（{why}）—— 拒绝继续")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        # user_agent 必须显式指定: Playwright 默认给 ``HeadlessChrome/156``（带 Headless
        # 标记 = 直球暴露自动化）。统一到 ``UA``（见文件顶部的真源说明）。
        # viewport 也设成与 UA 同一族的桌面尺寸，别用 Playwright 默认的 1280x720。
        ctx = await browser.new_context(locale="en-US", user_agent=UA, viewport={"width": 1920, "height": 1080})
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

        # ⚠️ 判据必须用 **ytcfg LOGGED_IN** —— 那是 YouTube 自己给 innertube 用的登录标志,
        # 与"取流能不能成功"是同一个体系。
        #
        # 以前这里查 DOM 节点存在性 (`#avatar-btn` / `ytd-topbar-menu-button-renderer` /
        # `button[aria-label*="Account"]` 里**任一**存在就算已登录), 有致命缺陷:
        #   `ytd-topbar-menu-button-renderer` **对未登录用户也存在** —— 顶栏的「登录」
        #   按钮就渲染在它里面。`.some()` 命中它 ⇒ **断言恒为 True**,
        #   于是 cookie 早就失效了脚本还报"登录态正常"（实测误报:
        #   同一环境下 avatar-btn=False / account_btn=False / sign_in_button=True,
        #   页面上是登录墙, 而断言照样打 True）。
        logged_in = bool(
            await page.evaluate(
                """() => {
                    try {
                        if (!window.ytcfg || !window.ytcfg.get) return false;
                        return window.ytcfg.get('LOGGED_IN') === true;
                    } catch (e) { return false; }
                }"""
            )
        )
        log(f"登录态断言(ytcfg LOGGED_IN): {logged_in}")
        if not logged_in:
            # 明确失败: 以前这里只打一行日志继续跑, 把失效的 cookie 原样写回,
            # 让调用方以为"刷新成功"。现在直接报错 —— 失效需要人工提供新 cookie。
            raise SystemExit(
                "cookie 不是登录态（ytcfg LOGGED_IN 非 true）—— 需要人工提供新的 YouTube cookie"
            )

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
        ok, why = is_login_state(ck)
        # ⚠️ 字段"在不在"**不代表登录态有效** —— 失效的 cookie 里这些字段照样在。
        # 真正的判据只有让 YouTube 自己说（ytcfg LOGGED_IN / 取流是否 OK），
        # 那需要开浏览器；这里只做静态检查, 所以要明确标注这一行的局限。
        print(f"静态判据(登录字段是否齐): {'通过' if ok else f'不通过 ({why})'} —— 不代表有效")
        print(f"上次刷新: {STAMP.read_text().strip() if STAMP.exists() else '从未'}")
        print("提示: 验证登录态是否**有效**请跑一次刷新（它用 ytcfg LOGGED_IN 断言）")
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
