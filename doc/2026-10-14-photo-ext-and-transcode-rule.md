# 图片发送：能直接发的不转码，后缀对齐 Telegram 白名单

日期：2026-10-14
起因：用户报「消息发送失败」—— 小黑盒链接
`xiaoheihe.cn ... link_id=970772a04ea8`（GTA6 新闻，2 张图）。

## 现象（活体证据）

161 生产日志 `02:19:52` / `02:21:12`：

```
编辑状态消息失败, 改为新建结果: PhotoExtInvalid: Telegram says: [400 PHOTO_EXT_INVALID] -
The extension of the photo is invalid. (caused by "messages.UploadMedia")
消息发送失败: ... PHOTO_EXT_INVALID
```

全库仅 4 条该错误，全部出自这一条链接。`流水线完成: processed_count=2` 说明解析与下载都正常，
**卡在最后一步上传**。

## 根因

URL 的 path 是 `.webp`，但 CDN 按 query 里那段 `format/jpg` **转成了 JPEG**：

```
ImageRef url=.../f151967b.../thumb.webp?imageMogr2/auto-orient/ignore-error/1/format/jpg/thumbnail/850x1450>
```

- `ImageRef.ext = image_ext_from_url(url)` 取 **path** 后缀 ⇒ 下载文件名 `.webp`
- 容器内实测：文件名 `.webp`、Pillow 读回 **`pil_format=JPEG`**
- 旧的 `process_image` 里 `needs_convert = image_format not in {"PNG", "JPEG"}` —— 见 Pillow 报
  `JPEG` 就**跳过转换**，原样把「JPEG 内容 + `.webp` 后缀」交给 Telegram
- 服务端**按扩展名判类型** ⇒ `400 PHOTO_EXT_INVALID`，整条消息（含正文）都发不出去

## 实测：Telegram 到底收什么（文档里没写，只能跑）

用容器内 pyrogram 走**生产同款** `messages.UploadMedia`（`sendPhoto` 走的是
`messages.SendMedia`，那条路径校验更松，不能代替）。

### 后缀白名单（只看扩展名）

| 后缀 | 结果 |
| --- | --- |
| `.jpg` `.jpeg` `.jfif` `.png` `.gif` `.bmp` | ✅ 接受 |
| `.webp` `.tiff` `.tif` `.ico` `.ppm` `.jp2` | ❌ `400 PHOTO_EXT_INVALID` |

### 内容层（内容格式 × 后缀）

| 内容 \ 出口后缀 | `.jpg` | `.png` | `.gif` | `.bmp` | `.webp` | `.tiff` |
| --- | --- | --- | --- | --- | --- | --- |
| JPEG | ✅ | ✅ | ✅ | – | ❌ | – |
| PNG | ✅ | ✅ | – | – | ❌ | – |
| GIF | – | ✅ | ✅ | – | – | – |
| BMP | – | ✅ | – | ✅ | – | – |
| WEBP | ✅ | ✅ | – | – | ❌ | – |
| TIFF | – | ❌ `IMAGE_PROCESS_FAILED` | – | – | – | ❌ |

⇒ **内容只要不是 TIFF 都能直接发**（服务端自己解码重编码）；**卡点全在扩展名**。

### 对照实验（排除"内容有问题"）

同一条链接下载到的两个真实文件，**同一份字节只改文件名**：

| 变体 | 结果 |
| --- | --- |
| `001_….webp` 原样（JPEG 内容） | ❌ `PHOTO_EXT_INVALID` |
| `001_….jpg`（字节完全相同，sha1 一致） | ✅ 发送成功 |

⇒ 与后缀直接相关，与内容无关。

⚠️ **探针踩过的坑**：把图缩成 320x220 再存成 `.webp` 时曾出现 "OK"，是命中了 Telegram 的
**内容去重**（同一份字节已上传过就跳过扩展名校验）。后续矩阵一律用**随机噪声**内容，每张都不同。

## 改动

按用户定的规则 —— **支持的格式直接发，不支持的再转**：

**① 白名单落成常量**（`utils/media_processing_unit.py`）

```python
_TG_PHOTO_SUFFIX = {          # 内容格式 -> 出口后缀（既在服务端白名单、又能直接解码）
    "JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp",
    "WEBP": ".jpg",            # 内容仍是 webp，只把名字落到白名单里
}
_TG_PHOTO_FORMATS = frozenset(_TG_PHOTO_SUFFIX)   # 需要本地重编码的 = 表外的
```

**② 原来的"多余转换"去掉**：`needs_convert` 从 `not in {PNG, JPEG}` 改成 `not in _TG_PHOTO_FORMATS`
—— 旧行为会把 WEBP / BMP **整张重编码成 JPEG**（画质损失 + CPU），而服务端本来就能直接解码。
同理去掉 `needs_rgb`（RGBA 强制转 JPEG）：实测 `PNG(RGBA)+.png`、`WEBP(RGBA)+.png` 服务端都收。

**③ 新增 `_align_tg_photo_ext`**：只**改名**不重编码，在 `process_image` 的所有出口统一调用
（GIF 的早期返回、填充/切割产物、普通路径）。后缀与内容不符时改名；内容不可解码
（TIFF…）时仍走原有的 `_img2jpg` 重编码。

**④ GIF 分支**：保留"不重编码"（转 JPEG 会丢帧），但出口也做后缀对齐 ——
GIF 内容被命名成 `.jpg` 时改回 `.gif`。

## 验证

**离线**（`test/test_media_processing_unit.py`，14 例）

新增 6 例，覆盖"能发就不转码"这条判据：
- WEBP 内容 → 产物后缀 `.jpg`、内容**仍是 WEBP**、字节一字不差（旧实现会被转码 ⇒ 用例会红）
- BMP 内容 → 原文件原样返回
- TIFF → 仍走重编码（服务端 `IMAGE_PROCESS_FAILED`）
- GIF / GIF 内容被命名成 `.jpg` → 不重编码、后缀改回 `.gif`
- JPEG 内容 + `.webp` 名、PNG 内容 + `.jpg` 名 → 改名到 `_TG_PHOTO_SUFFIX`
- 后缀已正确 → 原路径返回、不产生多余文件

**反向验证**：把 `_TG_PHOTO_FORMATS` 改回 `{"JPEG","PNG"}` 并把 WEBP 移出出口表，两条新用例
立刻失败（`test_webp_is_sent_as_is_only_renamed` / `test_bmp_is_sent_as_is`）⇒ 用例真的钉在
旧行为上，不是摆设。

全量：root `638 passed`；`ruff` + `pylint --errors-only` 干净。

**真机**（部署后，161 容器，同一条报障链接）

- 端到端 `ParseService().parse → download → process_media_files`：
  两个产物 `suffix=.jpg 内容=JPEG`，后缀在白名单、内容可解码 ⇒ PASS
- 生产发送链 `build_rich_media` → `send_rich_message` 到用户私聊：**SEND OK message_id=1402**
  （修复前同一条链接必失败）

## 未覆盖

- **AVIF / HEIF 内容**未测（本机 Pillow 存不出这两个格式的样本，需外部文件）。
  代码路径是"表外 ⇒ 本地重编码"，与 TIFF 同一条，但**没有真实样本验证过**。
- `.jfif` 在白名单里但我们会把 JPEG 内容统一叫 `.jpg`，所以生产不会出现 `.jfif`（不影响）。
- 视频/动图（`AniFile` → `InputMediaAnimation`）走的是另一条 UploadMedia 校验，本次只测了照片。

## 计划归档

本次的计划文件（`~/.hermes/plans/fix-photo-ext-invalid-xiaoheihe.md`）随本文档一并留档：

- phase1 测试（RED）：先写"WEBP 不重编码"的用例，确认在旧行为下**失败** ✅
- phase2 实现（GREEN）：白名单常量 + `_align_tg_photo_ext`，全量测试 + check.sh ✅
- phase3 真机验证：同一条链接端到端 + 生产发送链 ✅
- phase4 提交文档：本次提交 + 本文档 ✅

计划执行中的一处修正：初版实现只做了"后缀跟真实格式走"（`_align_ext_with_format`），
用户指出「所有图片都转 jpg 有点多余，支持的不用转」⇒ 改成**白名单直接发 + 只改名**，
并把 `needs_convert` / `needs_rgb` 这两处多余转换一并去掉。
