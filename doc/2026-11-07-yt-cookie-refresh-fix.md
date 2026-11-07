# YouTube cookie 刷新三条链路修复

日期：2026-11-07
触发：`https://youtu.be/UoVIgIpRr-U` 卡在 `attestation rejected after 10 attempts`

## 诊断结论（已验证）

| 事实 | 证据 |
| --- | --- |
| 生产配置 cookie **有效** | `GET /` → `LOGGED_IN: true`，26 项含 `SAPISID` |
| 刷新种子 cookie **残缺** | `/root/yt-cookie/cookies.json` 仅 20 项，缺 `SAPISID`/`SID`/`HSID`/`SSID` |
| 刷新脚本连续失败 60 次 | 10-23 15:15 后每 6h 失败，`ytcfg LOGGED_IN: False` |
| PO token 身份错位 | 缓存哈希 `f28b575d` ≠ 配置哈希 `2ccb6bf5` |
| 无告警 | 脚本无任何通知通道，只写 journal + cron.log |

**根因**：刷新脚本把**所有 cookie 注入到两个域**（`.youtube.com` + `.google.com`）。
`SAPISID`/`SID` 属于 `.google.com`，被重复注入到 `.youtube.com` 后被 Chromium 丢弃，
导出时只剩 `LOGIN_INFO` + `__Secure-3*`（会话令牌）⇒ 下次刷新必然 `LOGGED_IN: false`。
**10-23 那次"成功"的刷新自己把长期登录项洗掉了，之后进入死循环。**

## 三条要修

### A. 刷新脚本按域注入（根因，最关键）

- 按 cookie 的真实归属分域，不再盲目双注
- 缺 `.google.com` 域 cookie 时拒绝写回（防再次洗白）
- 写回前断言「项数不减少」「`SAPISID` 仍在」

### B. token 生成与取流用同一份 cookie

- 确认 `sabr.mjs` 收到的 cookie 是**配置里那份**（26 项），不是种子文件
- token 缓存哈希必须与配置 cookie 哈希一致

### C. 刷新失败要能通知到人

- 连续失败 N 次后发 Telegram 到你 DM
- 消息里写清楚：需要人工提供新 cookie

## 执行顺序

phase0 备份现状（生产配置 + 种子文件 + 脚本）
phase1 修 A，用真实 cookie 验证「刷新前后项数不减、SAPISID 保留」
phase2 修 B，验证 token 哈希 == 配置哈希
phase3 修 C，造一次失败验证消息真的发出
phase4 端到端：发一条 YouTube 链接，验证下载成功

## 铁律

- 生产配置改前先备份，用 Python 写 + 回读校验
- 每步验证用**真实产物**（`LOGGED_IN` 值、cookie 项数、token 哈希、ffprobe）
- 验收标准是「YouTube 链接能下下来」，不是「脚本退出码 0」
