# 公众号文章正文图与原生排版修复

**目标：** 修复 `https://mp.weixin.qq.com/s/K6cg6dx0zE0Jm16xAN7Fgg` 的丢图和章节排版，只改 Weixin 适配与其下载约束。保留兼容 CLI JSON 和现有原生富文本发送通道。

## phase0 — 原始证据

从生产 Bot 原始响应确认 `cgiDataNew.content_noencode` 与页面 `#js_content` 均有 33 张图。旧 CLI 解析和 `Document` 的图引用都已经完整；实际 `ParsePipeline` 却以 `requires_media_download=False` 返回空媒体。四组居中双段章节在源 HTML 用全段加粗首行及副标题表示，旧 `Document` 把首行一律当普通段落。

## phase1 — 失败回归

真实 HTML 结构的缩减样本覆盖标题、副标题、行内粗体、字面字符、普通图片说明及图的正文位置。旧代码验证五项失败：章节块、结果下载标记、流水线下载及两层旧缓存。用同一个库结果和原生 SDK 上行块验证冷／热路径。

## phase2 — 修复

仅在 Weixin 文章适配中把符合该结构的首行映射为 `heading`，不把任意居中段或部分加粗改成标题，不改原始 Markdown 投影。`WXArticleParseResult` 明确要求下载正文图片，原样走生产流水线。统一写版本升到 16，仅旧微信条目在两层按未命中处理。

## phase3 — 下载来源约束

评审发现原生富文本开启下载后必须约束来源。公众号入口使用精确域名和 HTTPS，正文懒加载图仅接受已知微信图片 CDN，不使用外部图片或任意后缀；公众号文章的文件名获取、范围探测、单文件及分块请求均禁跟跳。其它平台默认不变。假响应验证 HEAD/GET 不跟随 302；旧 HTTP 链接直接升级为原站 HTTPS，不多发页面请求。

## phase4 — 真实链接及回归验收

- 用最终代码走真实 `parsehub d`：33 个文件，10 JPEG、14 PNG、9 GIF，共 7,955,213 字节；逐一验证真实图片，且与生产 HTML 重放的下载 SHA-256 完全相同。
- 用这些文件经过真实媒体处理、Kurigram 富文本序列化与 TL 往返：33 个图块按源索引排列，4 个二级章节标题，现场与持久缓存的 TL 包逐字节一致。仅在 Telegram 上传处用合成 file_id；没有实发，不代表客户端显示已验证。
- 库 1365 passed、1 skipped、129 subtests passed；Bot 1060 passed、既有 14 项同用例同错误；Ruff、语法和差异检查通过，Pylint 仍仅同一项既存 `downloader.py E0611`。

## phase5 — 归档

验据为本文件随修复落入同一个本地 SSH 签名提交、`git verify-commit` 和提交差异检查。未动生产 Bot、Redis 或部署。
