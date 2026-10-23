# YouTube SABR：注入 PO Token（attestation）恢复取流

## 背景（2026-10-22 实测）

用户报「下载错误：SABR 取流失败 … `Cannot proceed with stream: attestation required`」。

复现与排除：
- 两条视频（`_QCcVv1sBYM`、`Nbc7I6y97FI`）× 三组 cookie（新 / 空 / 旧）**六次全部失败** ⇒ 与 cookie、账号、出口 IP 无关；
- 错误来自 googlevideo 的 `SabrStream.handleStreamProtectionStatus`：服务端下发
  `STREAM_PROTECTION_STATUS = 3`，即要求 **attestation（PO Token）**；
- googlevideo 只提供 `SabrStream.setPoToken()` 接口、**不含生成器**（4.1.1 / 5.0.1 都是）；
  最新版 5.0.1 也没有内置。

## 方案（用户选 A）

用 `bgutils-js`（bgutil-ytdlp-pot-provider 的官方依赖，2026-10 仍在维护）在本地生成
**content-bound PO token**（绑定 videoId），注入 `SabrStream`。

已验证可行性：容器内跑 bgutil server 的 `generate_once.ts -c _QCcVv1sBYM` 成功产出
`{"contentBinding":"_QCcVv1sBYM","poToken":"…","expiresAt":"…"}`（约 6h 有效期）。

**不复制第三方代码**（项目原则：优先依赖官方包）：只加 npm 依赖
（`bgutils-js` / `jsdom`），我们自己写一层 ~180 行的胶水 `tools/yt-sabr/gen-pot.mjs`。

## 阶段

### phase0：`gen-pot.mjs` 独立可用
- 产出：`tools/yt-sabr/gen-pot.mjs`（`bun gen-pot.mjs <videoId>` → stdout 一行 JSON `{poToken, visitorData?, expiresAt}`）
  - 流程：GET `https://www.youtube.com` → 提取 `ytcfg.set({...})` + `window.ytAtN({...})` challenge →
    加载 interpreter JS → `BotGuardClient.snapshot()` → `POST att/get`（GenerateIT）→ `WebPoMinter.create()` →
    `mintAsWebsafeString(videoId)`
  - `package.json` 加 `bgutils-js`、`jsdom`
- 验证：容器内 `bun gen-pot.mjs _QCcVv1sBYM` 输出 poToken（长度 > 100），且**同一 token 可重复生成**。

### phase1：`sabr.mjs` 支持 poToken
- 产出：`sabr.mjs` 读 `input.poToken`，在 `start()` **之前** `stream.setPoToken()`（已加）。
- 验证：手工把 phase0 的 token 塞进输入 JSON，跑一条纯 SABR 视频 → 下载成功。

### phase2：自动生成（无感）
- 产出：`sabr.mjs` 在未提供 `poToken` 时**自己调用** `gen-pot` 逻辑（同进程 import，不起子进程）；
  生成失败 ⇒ 明确报错（`poToken generation failed: …`），不静默。
- 验证：同一条视频，输入**不带** poToken 直接跑 → 成功；日志出现 `注入 PO token`。

### phase3：测试 / 部署 / 真机
- 产出：Python 侧不改签名（`fetch_sabr_streams` 仍只传现有字段），错误信息透传；
  单测覆盖 `gen-pot` 的参数拼装与失败分支（不联网的部分）。
- 验证：`pytest lib/test/` 全过；部署 161；生产解析 `_QCcVv1sBYM`（纯 SABR）成功且文件可播放
  （ffprobe 时长/编码核对）。

## 风险与回退

- **BotGuard 更新**：`bgutils-js` 需要跟进；生成失败时错误要显式暴露（不吞）。
- **额外耗时**：每视频 +1–2s（token 生成）；可对同一 videoId 做**短时缓存**（有效期 ~6h）。
- **回退**：`input.poToken` 为空时行为与现在一致（会失败）—— 不存在破坏性回退路径；
  真要回退就还原 `sabr.mjs` 的两处改动。

## 完成状态（2026-10-22）

- phase0 ✅ `gen-pot.mjs` 可用（`bun gen-pot.mjs _QCcVv1sBYM` → poToken len=124~128，629–880ms）
- phase1 ✅ `sabr.mjs` 注入 `setPoToken`，手工 token 取流成功（17.9MB av1 1080p）
- phase2 ✅ 未带 token 时自动生成（813ms）+ `/tmp/yt-pot-cache` 跨进程缓存（第二次命中 0 成本）
- phase3 ✅ `fetch_sabr_streams` 端到端（`Nbc7I6y97FI` 21.4MB av1 1080p）；已部署 161 并验证镜像

提交：`3aa0636`（feat）+ `30bdd03`（bun.lock）；skill 已记入 `references/youtube-sabr.md`。
