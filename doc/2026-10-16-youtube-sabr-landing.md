# YouTube SABR 落地（已完成）

用户要求：`Nbc7I6y97FI` 这类视频的**高清流走 SABR**（`serverAbrStreamingUrl`），
拿到最高画质（1080p）。

**最终方案（用户决定）**：不再自研 SABR 协议，**直接用 `LuanRT/googlevideo` 的
`SabrStream`**（TypeScript 库，bun 运行），Python 侧只负责备料和收产物。

## 实施（已完成）

| 阶段 | 产物 | 状态 |
| --- | --- | --- |
| tools/yt-sabr | `sabr.mjs` + `package.json` + `bun.lock`（依赖 googlevideo@4.1.1） | ✅ 容器内实测 1080p h264 + AAC |
| provider_api/youtube_sabr.py | `fetch_sabr_streams`（子进程封装） | ✅ 12 个离线测试 |
| youtube_video.py | `SabrSource` + `extract_sabr_source` + `_apply_nsig(extra_urls)` + `select_streams` SABR 分支 | ✅ 测试覆盖 |
| parsers/parser/youtube.py | `_download_via_sabr`（拉流 → ffmpeg mux） | ✅ |
| Dockerfile.deploy | bun（已有）+ `bun install --frozen-lockfile` | ✅ 镜像构建通过 |

## 关键协议事实（自研时踩的坑，现由库处理 —— 记录备查）

1. **rn 从 0 开始**：第一个请求必须 `&rn=0`，从 1 开始服务端只回策略帧
2. **UMP 用自定义 varint**（UTF-8 风格：<128=1B / <192=2B / <224=3B / <240=4B / else 5B），
   **不是 LEB128**
3. **FormatId.last_modified 是 int64 varint**，不是字符串
4. **SABR_REDIRECT(part 43)**：服务端会要求换 URL，必须处理
5. **`sabr.no_audio_selected`**：selected_audio_format_ids 要带 xtags
6. `selectedFormatIds`（body #2）首次为空，FORMAT_INITIALIZATION_METADATA 之后才填
7. `playerTimeMs` 是顶层 body #4（不在 abrState 里）
8. playerTimeMs 只在真正下载到数据后才推进

## 验证记录

- 容器内 bun 工具直接跑：`video=44657428B (itag 137) audio=1460041B (itag 140)`
- 端到端（生产入口 `ParseService().parse` + `download`）：
  42.24MB mp4，`ffprobe: h264,1920,1080 + aac,44100,2`，耗时 9.5s

## 提交

- `e2b5145` feat(youtube): 高清流走 SABR —— 用 LuanRT/googlevideo 取流（不再自研协议）
- `738a815` fix(yt-cookie): 登录态判据对齐 yt-dlp
