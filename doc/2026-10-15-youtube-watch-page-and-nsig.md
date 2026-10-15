# YouTube 取流：POST player → GET watch 页 + n-sig + cookie 自动化

日期: 2026-10-15
相关提交: `f42f67a`（取流改 watch 页 + 配置热重载 + cookie 写入脚本）、`399723b`（watch 页的 playerId 只在 HTML 里）

## 症状

用户报的新视频（`csSN8XuN1u4`）解析失败，`YoutubeVideoError: player API 解析失败:
VISIONOS: LOGIN_REQUIRED: Sign in to confirm you're not a bot`。老视频正常，新视频全挂。

## 根因（三层，逐层剥开）

### 第 1 层：取流接口选错了

自研路径一直在 `POST /youtubei/v1/player`，但**浏览器从不发这个请求** ——
它只 GET watch 页，服务端把 `ytInitialPlayerResponse` 内嵌在 HTML 里
（早期 CDP 抓包 0 个 player 请求就是证据，当时没读懂）。

受控对照（同一视频、同一 cookie）：

| 路径 | 直连 | 走代理 |
| --- | --- | --- |
| POST `/youtubei/v1/player`（VISIONOS / TVHTML5 / WEB_EMBEDDED 全试过） | LOGIN_REQUIRED | LOGIN_REQUIRED |
| **GET watch 页** | **OK / 31 条明文流** | **OK / 31 条明文流** |

- **出口 IP 无关**（直连 IPv6 与代理都成功）。
- **浏览器 UA + cookie 是必需项**：不带 cookie 时两条路一起挂。
- watch 页一次同时给 `streamingData` 与 `microformat`（发布时间/点赞/上传者），
  所以解析从 2 次请求降到 **1 次**（原来 WEB 取元数据 + VISIONOS 取流）。

### 第 2 层：n-sig（直链 403）

watch 页给的直链里 `n=` 是需 JS 变换的令牌，原样请求必 403。实测：

- 原始 n → **403**
- 解后 n → **206 / 2048B**

用 `yt-dlp-ejs` 的纯 JS 求解器（lib.min.js 含 meriyah 解析器，Unlicense）在 **bun**
里跑（bun 优先、node 兜底；两者解出的值逐字符一致，bun 冷启动 0.9s vs 1.2s）。
播放器 JS 按 playerId 跨进程缓存（6h TTL）—— n 变换只由播放器版本决定，是**内容无关**
的身份值，稳态不增加每内容请求。

**只能改 URL 里 `n` 的值**：`sig` 覆盖 `sparams` 列出的那组参数，改任何一个都 403
（逐项替换 id/n/sig/lsig/ns/spc 全部失败）。

### 第 3 层：watch 页的 playerId 只在 HTML 里

内嵌的 `ytInitialPlayerResponse` **没有 `assets` 字段**（实测 30 条流、assets 不存在），
而 playerId 原来只从 `assets.js` 取 ⇒ 恒为空 ⇒ n-sig 整条链短路
⇒ **解析"成功"但下载 403**（这个"看着成功、下载失败"的形态最容易骗过人）。

修法：`player_id_from_response(data, html)` 两条来源都看 —— 结构化 assets 优先，
退回页面 HTML 里的 `/s/player/<id>/`（多处时取出现次数最多的）。

## cookie 生命周期

- **刷新**：宿主机 161 的 `/opt/yt-browser/refresh_cookie.py`（Playwright + Chromium）
  带登录态加载 youtube.com，让 Google 滚动续期（`__Secure-*SIDTS` 只在页面请求里更新）。
- **接线**：`refresh-and-push.sh` → `push_cookie_to_bot.py` → 仓库内
  `scripts/set_youtube_cookie.py`（原子写 + 回读校验 + 备份 + 保持 600）。
- **定时**：`yt-cookie-refresh.timer`，每 6 小时（`OnBootSec=10min`、抖动 15min、
  `StartLimitBurst=3` —— 刷新失败不要无限重试）。
- **生效**：靠**配置热重载**（见下），不用重启容器。

⚠️ **能力边界**：刷新脚本只能"续期"，**不能重新登录**。种子 cookie 被踢下线时
（改密码/安全策略）`SID/HSID/__Secure-1PSID` 缺失，脚本直接 `SystemExit` ——
那时必须人工提供一份新 cookie。无头浏览器做不到"给账号密码自己登"（有设备验证/2FA）。

## 配置热重载

`pl_cfg` 原是模块级加载一次，刷新 cookie 后要重启容器才生效。
新增 `PlatformsConfig.reload_if_changed()`：**按 `st_mtime_ns` 惰性重载、原地更新字段**。

- **原地更新是硬要求**：`from core import pl_cfg` 的引用到处都是，换对象会让它们一直用旧配置。
- 用纳秒 mtime —— 同一秒内写入不会漏。
- 新文件坏掉时**保留旧配置**、记日志，且不反复重试同一份坏文件。
- 惰性触发而不是 SIGHUP：刷新脚本改完文件后**下一次解析自动生效**，不需要外部发信号。

## 验证（真机，非推断）

- n-sig：原始 n `403 / 0B` → 解后 n `206 / 2048B`（容器内实跑）
- 端到端：报障链接解析成功，元数据齐全（发布时间 `2026-10-15 04:00:28-07:00` /
  点赞 126 / 播放 2147），**下载落盘 0.58MB mp4**（ffmpeg 合并成功，说明走的是分离流）
- 刷新链路：`cookie字段数=24`、`代理关闭=True`、timer 下次触发 6 小时后、
  本次 `status=0/SUCCESS`
- 测试：lib 988 passed；阴性对照确认破坏主路会让 6 个测试变红

## 容易踩的坑

1. **别再用 POST player 取流** —— 换 client、补 `playbackContext`、加
   `X-YouTube-Bootstrap-Logged-In` 全试过，一律 `LOGIN_REQUIRED`。
2. **watch 页的 playerId 不在响应里**，只在 HTML 里。
3. n-sig 解算失败的表现是"解析成功但下载 403"，不是解析报错。
4. 热重载是**惰性**的：改完文件后要等下一次真实解析才生效。
5. cookie 只能是登录态；`VISITOR_INFO1_LIVE` 那类匿名 cookie 不够。
