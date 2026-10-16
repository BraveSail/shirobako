# YouTube SABR 落地（已完成）

用户要求：`Nbc7I6y97FI` 这类视频的**高清流走 SABR**（`serverAbrStreamingUrl`），
拿到最高画质（1080p）。

**最终方案（用户决定）**：不再自研 SABR 协议，**直接用 `LuanRT/googlevideo` 的
`SabrStream`**（TypeScript 库，bun 运行），Python 侧只负责备料和收产物。

## 实施（已完成）

| 阶段 | 产物 | 状态 |
| --- | --- | --- |
| tools/yt-sabr | `sabr.mjs` + `package.json` + `bun.lock`（依赖 googlevideo@4.1.1） | ✅ |
| provider_api/youtube_sabr.py | `fetch_sabr_streams`（子进程封装，含编码参数透传） | ✅ 16 个离线测试 |
| youtube_video.py | `SabrSource` + `extract_sabr_source` + `_apply_nsig(extra_urls)` + `select_streams` SABR 分支 | ✅ |
| parsers/parser/youtube.py | `_download_via_sabr`（拉流 → ffmpeg mux → 容器感知输出） | ✅ |
| Dockerfile.deploy | bun（已有）+ `bun install --frozen-lockfile` | ✅ |

## 编码选择（用户定案：默认 av1 > vp9 > avc）

`DEFAULT_SABR_CODEC_PRIORITY = ("av1", "vp9", "avc")`（在 `youtube_video.py`）。

库自带的 `chooseFormat` **做不到**这种次序（只有 `preferH264`/`preferWebM`/`preferMP4`
三个布尔，同分辨率按数组顺序取 ⇒ 实际等于 AVC 优先）。工具内自己按 `codecPriority`
逐编码找候选、以显式格式传给 `start({videoFormat, audioFormat})`。

**实测（1080p，90s 视频）**：

| 编码 | itag | 体积 | 容器 | 音频 |
| --- | --- | --- | --- | --- |
| av01.0.08M.08 | 399 | 20.4MB | mp4 | aac (140) |
| vp9 | 248 | 19.3MB | webm | opus (251) |
| avc1.640028 | 137 | 44.7MB | mp4 | aac (140) |

⇒ **AV1 体积约为 AVC 的 47%**（用户选它做默认的原因）。

降级方式：`SabrSource(codec_priority=("avc",))`；最直接的是 `video_itag`/`audio_itag`。

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

## 子进程封装的四个坑（集成时逐条做，每个都实测踩过）

1. ★ **写文件必须 `await` write 回调 + `end()` 等 flush，并校验「落盘字节 == 流累计字节」**。
   只调 `out.write(chunk)` 不等回调就 `process.exit(0)` —— 数据还在缓冲区里，
   同一视频落盘 44MB / 27MB / 26MB 不定，而工具报告始终 44MB ⇒ **静默交付截断的视频**。
2. ★ **输出 JSON 后等 stdout flush 再 `process.exit()`** —— stdout 接管道时是异步的，
   `console.log` 后立刻 exit 会把错误信息整个丢掉（现场只剩「退出码 1、stderr 空」）。
3. **工具的错误信息在 stdout，不在 stderr**（一行 `{"ok":false,"error":…}`）。
   Python 封装拼错误时必须两者都带，只读 stderr 就退化成无信息量的「退出码 N」。
4. ★ **传给子进程的路径（输入 JSON / 输出目录）必须 `resolve()` 成绝对路径** ——
   子进程的 cwd 是工具目录，相对路径（生产 `bs.download_dir="downloads"`）会被它按
   自己的 cwd 解析 ⇒ `ENOENT`。**这个坑恰好测不出来**（验证时顺手用了 `/tmp/...` 绝对路径）。

**产物完整性判据取「媒体时长 == 源时长」**（`ffprobe -select_streams v:0`），
不是「退出码 0 / 字节非零」—— 截断那次视频流只有 55s（源 90s）。

## 验证记录

- 容器内 bun 工具直接跑（三种编码各一次，均通过字节/时长校验）：
  - avc 137+140 → 44.7MB + 1.4MB
  - vp9 248+251 → 20.3MB + 1.5MB（webm 容器）
  - av1 399+140 → 21.4MB + 1.4MB
- 连测 5 次（修截断 bug 后）：报告字节 == 落盘字节 == 时长 90.09s，5/5 通过
- **端到端（生产入口 `ParseService().parse` + `download`，不传任何编码参数）**：
  默认 `('av1','vp9','avc')` 生效 → **21.79MB mp4**，`ffprobe: av1,1920x1080,90.09s + aac`，8.5s
- 相对路径场景（`bs.download_dir="downloads"`）复现 + 验证修复 → 43.99MB 落盘成功

## 提交

- `e2b5145` feat(youtube): 高清流走 SABR —— 用 LuanRT/googlevideo 取流（不再自研协议）
- `738a815` fix(yt-cookie): 登录态判据对齐 yt-dlp
- `5ba2b9a` fix(yt-sabr): 失败时报出工具完整 stdout/stderr
- `2aa3551` fix(yt-sabr): 等待 write 回调 + 落盘校验 —— 修静默截断
- `04a8d13` fix(yt-sabr): output_dir 必须绝对化 —— 相对路径会被子进程按工具目录解析
- `a9b65a1` feat(sabr): 编码优先级（av1>vp9>avc）+ 容器感知输出
- `a064c04` feat(sabr): 默认编码优先级 av1 > vp9 > avc（用户定案）
