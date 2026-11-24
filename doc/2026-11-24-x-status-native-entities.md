# X status：平台实体直接走 Telegram 原生格式

范围：普通 status（含 note tweet、回复/引用、投票、trending 和链接卡片）、同帖 #t 与所有 Telegram 入口。独立 X Article 和其它平台保留现状。绝不从旧 Markdown/HTML 派生 X 正文；旧公开 CLI 输出可以保留为兼容字段，但不能作为已迁路径的渲染源。

## phase0 — 核对基线和边界
- [x] 基线 `b5d554a`，本地干净、与远端 main 相同；生产 161 `3589a2e` 正在服务。只读日志确认原帖已经进入实际流水线；离线检查生产 SDK 2.2.26 能构造并编解码 blocks。既有 `rich-segments-refactor.md` 和 `segments-per-platform.md` 是整站计划，本文件限定 X status。
- [x] 从 X provider 的真实正文/实体和 note richtext 入口接线；role 是业务数据，不能靠猜 `>`；新版两层缓存存同一份 DTO。独立 Article 不切通道。发现 X 正文还流向终稿、占位、inline 预览/选中、guest、持久缓存和 raw/zip caption。

## phase1 — ParseHub X 结构和结果缓存
- [x] 产物：`parsehub/types/x_status.py`、X provider/parser 与结果层内部序列化。冻结 DTO：`XStatus(main: XPost, reply: XPost|None, quoted: XPost|None, trending: XTrending|None)`；`XPost` 带 `segments: tuple[TextRun,...]`、独立署名/profile、`poll_index`、可选 YouTube 链接卡片；`TextRun(text, url='', styles=())`。trending 块带标题、摘要、链接和实际封面媒体索引。先切实体再解平台转义/展开 URL；note UTF-16 与译文码点分开。原公开 CLI 结果不改 schema。只给普通 status 增加 `x_status`，主文/引文的预备数据不靠 Markdown。不会新增抓取请求。
- [x] 验证：新增用例经 RED/GREEN；`PYTHONPATH=lib/src .venv/bin/python -m pytest -q lib/test --tb=short` 返回 1286 passed、1 skipped、128 subtests passed。

## phase2 — 原生渲染与译文
- [x] 产物：`plugins/parse/x_status.py` 原生 blocks、`x_caption.py` 原生 caption entities，分别构造对应能力。译文以 `TextRun` 列表缓存，按 role 替换请求副本；旧 Article/旧数据继续走旧逻辑。不添加原文折叠（实际基线未展示）。
- [x] 验证：生产同版 Kurigram 2.2.26 的 TL 往返；`test_x_status_blocks/caption/translation`、`test_translate_flag`、`test_x_rich_anchors` 共 110 passed。默认 .venv 为旧 pyrogram，bot 用隔离依赖和不可用假凭据验证。

## phase3 — 接通发送和持久缓存
- [x] 产物：共同 dispatcher 连接终稿、无媒体进度及预览、inline 选中、guest 和持久缓存；raw/zip + GIF 回退使用显式实体。两层 cache 写版本 14，旧 X status 精确判旧，X Article 和其它平台不失效；新缓存复用 `file_id`，保留原发送降级。
- [x] 验证：入口回归 125 passed（`test_x_status*`、guest cache、status reporter、cache version 与 inline media cache）。通过真实 CLI 入口回放该帖 `2124820266370900014` 的生产原始响应，在 provider 网络边界注入剪去无关字段的响应，不改 CLI 或业务逻辑；CLI exit 0，6 个媒体引用。真实 SDK 回读 inline 报文：4 个话题链接、`||` 字面保留、无泄漏的 `<a href=`、原生页脚和来源链接；未实发 Telegram。

## phase4 — 验收与交付
- [x] 产物：完整改动，通过独立审查并修正验收中的可达回归；本计划在 `doc/2026-11-24-x-status-native-entities.md` 随改动归档。
- [x] 验证：lib 全量 1290 passed、1 skipped、128 subtests passed；bot 全量 1022 passed、14 failures（与独立 HEAD 快照的失败 ID 和错误摘要相同）。Ruff 45 → 37，无新增；Pylint errors-only 无错误；语法和 diff 检查通过。最后的格式修正后再跑相关 parser 11 项通过。不推送、不部署 161，不声称 Telegram 客户端已验收。
- [x] 用英文 SSH 签名提交代码和归档；`git verify-commit HEAD`、归档存在性和干净工作树均已核对。
- phase4 闸门修订：phase2 已发现基线 bot 套件有 14 项既存失败。保留旧平台不变，不为得到全绿修改无关实现/断言；以同依赖、同配置的独立 HEAD 对照逐条确认失败集和原因相同，迁移及受影响测试必须全绿。
- phase4 审查修订：补齐冷热列表摘要读取显示副本的原生文本、重复实体位置判定、用户附言的原生实体传递、热缓存敏感动画打码与 guest 去封面重试；定向回归后重跑原验收。平台现有整条富消息长度上限另列为既存限制，不在本次架构迁移擅自引入截断。
