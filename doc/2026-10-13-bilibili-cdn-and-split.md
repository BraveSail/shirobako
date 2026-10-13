# B站下载优化：优先 bilivideo 副本 + 分片阈值 2MB

## 背景（2026-10-13 用户决定）

用户查看 B站下载历史数据后拍板两个改动。

### 依据：161 历史下载统计（bot.log 全量 18 条）

| CDN | 条数 | 平均 | 中位 | 最快 | 最慢 | 传输中断 |
|---|---|---|---|---|---|---|
| upos-*-bilivideo.com | 11 | **8.99 MB/s** | 2.26 | 35.30 | 0.16 | **0 次** |
| upos-hz-mirrorakam.akamaized.net | 7 | 3.42 MB/s | 1.16 | 12.03 | 0.09 | **6 次** |

且 B站会把 akamai 当首选发给某些视频（bilivideo 只在 backup_url 里）——实测那条
「异环」视频主地址 akamai 连断 4 次才切备用。

### 分片阈值问题

`min_split_size` 原为 10MB → <10MB 文件走单连接，而 CDN 对单连接限速
（8.1MB 文件实测 87 秒；同尺寸 4 连接可达 15+ MB/s）。

## 改动

### 1. B站地址选择：优先 bilivideo（`lib/src/parsehub/parsers/parser/bilibili.py`）

- 从响应给的副本（`durl.url` + `backup_url[]`）里**挑选** bilivideo 的地址；
- 其余副本（含被降级的原首选）全部进备用；
- **只在响应给的副本里挑** —— 绝不改写域名（签名与 host 绑定，改写 = 伪造，实测 403/超时）。
  响应里没有 bilivideo 副本时保持原样。
- 去重（首选与备用相同时不重复）。

### 2. 分片阈值 10MB → 2MB（`lib/src/parsehub/utils/downloader.py`）

- `SegmentDownloader.__init__` 与 `download()` 两处默认值都改；
- **全局生效**（所有平台）—— 用户明确要求；
- connections 保持 4（全链路本来就没有覆盖，生产日志实证 4 分片确实发生过）；
- 新行为：<2MB 单连接；2-4MB → 2 片；4-6MB → 3 片；≥8MB → 4 片（封顶）。

## 验证

- 单测：lib 956 + bot 630 全过。
  - `test_bilibili_video_url.py` 重写（9 条）：核心场景 = akamai 首选/bilivideo 备用时选 bilivideo；
    无 bilivideo 副本时保持原样；域名永不改写；去重。
  - `test_download_split_threshold.py` 新增（5 条）：默认值 2MB、`_should_use_multipart` 门控
    （恰好 2MB 走分片、差一字节单连接）、分片公式逐档、connections 封顶、显式覆盖仍生效。
- 真机（161，部署后）：那条「异环」视频（曾经 akamai 4 连断）现在**首选 bilivideo**、
  akamai 作备用；`min_split_size = 2.0 MB`。

## commit

`812cfb4`（perf(bilibili): prefer the bilivideo copy, split from 2MB）
