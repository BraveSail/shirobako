# Local modifications

This repository is derived from two upstream projects and is an independent project now
(it is not a GitHub fork of either):

- [z-mio/parse_hub_bot][upstream-bot] — the bot, at the repository root
- [z-mio/ParseHub][upstream-lib] — the parser library, vendored under `lib/`

The library used to live in a separate repository and was pulled in as a published package
plus a build-time overlay; it is now in-tree (`lib/`, added with `git subtree`, full history
preserved) and resolved as a uv workspace member.

Below is what differs from upstream, grouped by topic. Every item has a matching commit in
`git log upstream/main..main`, or — for the library — the corresponding commit under `lib/`.

## 1. 发送形态：富文本（rich message）是唯一路径

上游用「媒体组 + caption」发送：媒体堆在顶部，正文当 caption，排版被压平。本仓库改成
Telegram 的富文本消息（`sendRichMessage`），正文按原文 markdown 还原，统计与来源进页尾。

- **HTML 锚点**：footer 与正文里的链接必须写 `<a href="url">文字</a>` —— 富文本的 markdown
  链接语法在 footer 块里不生效，会原样显示成 `[文字](url)`。
- **页尾统计行**：发布时间 + 浏览量 + 点赞数，**一律 24 小时制**（中文只有日期部分是中文格式，
  如 `19:00 · 2026年10月3日 · 4,985 查看 · 158 点赞`），拿不到的项直接不显示，不留空占位
  （threads 没有浏览量但有点赞时只显示 `303 点赞`）。
- **标签行**：平台提供的标签渲染成指向该平台标签页的链接；行首 `#标签` 必须转义
  （否则被当成一级标题，字号巨大），裸 hashtag 遇到 `・` 之类的字符会被服务端截断，
  链接形式两个问题都没有。标签多时按显示宽度截断（`TAG_LINE_DISPLAY_BUDGET`）。
- **多图 = 图集**：正文里并列多个媒体占位会被渲染成各自独立的图片，必须包进
  `<tg-collage>`（blocks 路径用 `InputRichBlockCollage`）。
- **单换行会被吞成空格**：行尾补两个空格才保留换行（threads 这类一行一条的排版依赖它）。
- **删除的路径**（`feat!: rich message is the only send path`）：
  - 「媒体组 + caption」老发送路径
  - `rich_mode` 开关（不再有第二种模式，开关无意义）
  - 长文的 Telegraph 路径（富文本已能直接还原 markdown 正文）
  - `/raw`、`/zip` 两个显式命令模式**保留**（用户敲命令才走，不是默认路径）
- **缓存路径同样走富文本**：`send_cached` 与 inline 共用 `build_cached_rich_content`，
  从缓存字段重建同一套排版（否则同一链接第二次发送会变成另一种格式）；发送后把服务端
  返回的媒体 file_id 写回缓存，下次发送零上传。

## 2. inline 模式

- **一个链接只给一项**（`RICH_RESULT_ID`），Media 走「占位 → 用户选中 → 下载上传 → 编辑」
  流程：回答查询时不能带外链媒体（Telegram 回 `400 EXTERNAL_MEDIA_NOT_SUPPORTED`），
  编辑（`messages.EditInlineBotMessage`）不受这条限制。
- **不要用 `InlineQueryResultPhoto` 做封面占位**：那样消息本体就是「一张图 + caption」，
  富文本的排版 / 图集 / 标签 / 页脚全部丢失。封面只当结果列表的缩略图（`thumb_url`）。
- **键盘的用途**：Telegram 只在消息带 inline keyboard 时才回传 `inline_message_id`，
  这是二次编辑的前提；选中后立刻用 `ReplyKeyboardHide` 摘掉（空的 `ReplyInlineMarkup` 非法）。
- **兜底**：`answer()` 被拒时先剔掉富文本项重试，再回落纯文字提示 —— 拒答发生在
  `answer()` 这一步，不兜底客户端只会一直转圈。
- **折叠**：长正文按 350 字符 / 8 行折叠（`<blockquote expandable>`，不可嵌套）。

## 3. 元数据与作者

- **库侧新增字段的消费端**（`lib/src/parsehub`）：`published_at` / `view_count` /
  `like_count` / `author_handle` / `author_url` / `tags`。每个字段的落地清单是
  库 → 直发路径 → 缓存路径（`build_rich_markdown_by_str`）→ `CacheParseResult` →
  「缺字段即过期」的重解析判据，漏一处就是「有时候有、有时候没有」。
- **作者行**：`名字 @handle：`，其中 `@handle` 链到作者主页；显示名与用户名相同时只写
  `@handle`。主页地址由库的 `profile_url(platform, handle=, user_id=)` 按模板生成。
- **引用 / 回复块**：整块斜体、**不写「引用」「回复」字样**（引用块本身已表明关系）、
  作者 handle 链到主页；主推自己的媒体排在引用卡片**之上**（与 X 的观感一致）。
- **跨平台统一**：引用块 / 作者标签 / 主页地址的排版全走库里的公共 helper
  （`format_quote_block` / `format_author_link` / `profile_url`），平台解析器只取字段，
  不再各写一份。

## 4. 敏感内容遮罩

- 平台有官方标记才打码（pixiv `xRestrict > 0`、twitter `possibly_sensitive`），不靠关键词猜。
- 直发：`has_spoiler`。
- 富文本：官方 API 的富文本媒体块**没有 spoiler 字段**，敏感内容自动切到自造的 raw
  blocks 路径（`plugins/parse/rich_blocks.py`，`PageBlockPhoto/Video(spoiler=True)`）。
- inline：占位封面那截打不了码（`InlineQueryResultPhoto` 走 `InputWebDocument`，协议层
  没有该字段）；选中后编辑成的富文本那截**能**打码（必须走 blocks，markdown+media 路径会静默丢遮罩）。

## 5. 构建与部署

- **单仓库构建**：库是 uv workspace 成员（`lib/`），`uv lock` 把 `parsehub` 解析到本地目录
  （lock 里 `source = { editable = "lib" }`），镜像里 `COPY lib/ ./lib/` + `uv sync --frozen` 即可，
  **不再需要 `additional_contexts` 或构建期覆盖安装**。
  - 历史坑（已作废，别再往 lock 里写 git 源）：builder 镜像**没有 git**，`uv sync --frozen`
    遇到 git 源会直接失败 —— 这也是当初要搞「PyPI 占位 + 构建期覆盖」的原因。
- `Dockerfile.deploy` 统一用于生产本地构建和 GHCR CI（含 Bun/SABR）；`Dockerfile` 为通用构建。
- `compose.deploy.yaml` 保留本地构建方式；`docker-compose.yaml` 使用 `ghcr.io/bravesail/shirobako`
  预构建镜像，两者项目名、宿主网络、数据/日志挂载与日志轮转一致。发布标签规则见 README。

## 6. HTTP 客户端统一走 curl_cffi（`lib/src/parsehub/utils/http.py`）

所有 provider 与下载器的请求都经由一个薄封装层，底层是 **curl_cffi**（浏览器 TLS 指纹，
统一 `IMPERSONATE = "chrome150"`）。这不是性能优化 —— 部分是**能不能访问**的问题：
Cloudflare 前置的站点（如 linux.do）在**同一份 cookie** 下，普通 HTTP 客户端拿到 403 挑战页，
curl_cffi 拿到 200。

- 封装层保持原有调用形态与异常名（`AsyncClient` / `HTTPError` / `HTTPStatusError` /
  `TimeoutException` / `NetworkError` / …），并补齐了 curl_cffi 缺失的两个能力：
  `aclose()`（它只有同步的 `close()`）与 `is_closed`；`follow_redirects` 在会话级与请求级
  都翻译成 `allow_redirects`（curl_cffi 默认跟随重定向，与原来的行为相反）。
- 下载器的流式读取改为 `stream=True` + `aiter_content()`（没有 `client.stream()` 上下文管理器形态）。
- `httpx` 已从运行时依赖移除（仅剩 i18n 构建工具的间接依赖，生产镜像里不存在）。

## 7. 平台侧改动（`lib/`，相对 z-mio/ParseHub）

- **帖子与作者元数据**（上层消费方依赖）：
  - `is_sensitive` —— 平台有官方标记才置位（pixiv `xRestrict > 0`、twitter `possibly_sensitive`），
    4 个 `ParseResult` 子类同参转发 + `to_dict()` 带上；**不靠关键词猜**。
  - `published_at` / `view_count` / `like_count` —— 取值来源：twitter `legacy.created_at` +
    `views.count`、threads `taken_at` + `like_count`、douyin `create_time` +
    `statistics.play_count`、bilibili `View.pubdate` + `stat.view`、yt-dlp `timestamp` +
    `view_count`、pixiv `createDate` + `viewCount` + `likeCount`。
  - `author_name` / `author_handle` / `author_url` —— 给所有支持平台补齐作者名与主页地址，
    只用**明确的作者对象**取值，**绝不从正文猜**（`utils/helpers.get_author_name`）。
  - `tags` —— 平台自带标签（pixiv `tags.tags[].tag` 等），归一化（去空、忽略大小写去重、
    保持原顺序）在库层完成。
- **引用 / 回复上下文**：
  - twitter：被回复（`in_reply_to_status_id_str`）与被引用（`is_quote_status` +
    `quoted_status_id_str`）**互不排斥**，各自生成；内嵌 `quoted_status_result.result` 可能被
    匿名请求降级成 `TweetUnavailable`，故有 `quoted_status_id` 兜底补取，失败只记 warning。
  - threads：**无需二次请求** —— 父帖与目标帖同处一个 `thread_items` 数组、紧邻前一条即父帖
    （仅在 `text_post_app_info.is_reply` 为 true 时取）。
  - t.co 还原：按 `entities.urls` 把短链换成 `expanded_url`。
- **公共排版 helper**（所有平台共用）：`format_author_label` / `format_author_link` /
  `format_quote_block` / `profile_url` + `PROFILE_URL_TEMPLATES`。
- **新增平台 linux.do（Discourse 论坛）**：读站点自带的话题 JSON（`/t/topic/<id>.json`）而非抓 HTML；
  映射标题 / 楼主帖正文（cooked HTML → markdown，展开 `details` 折叠壳、按媒体单独发送图片）/ 作者 /
  发布时间 / 浏览量 / 点赞 / 回复数 / 标签；`NSFW` 标签即平台自带敏感标记，驱动打码。
  **需要 cookie**（`cf_clearance` + `_forum_session`），配置在 `platforms.linuxdo.cookies`。
- **平台解析修复**：pixiv 整平台支持（尺寸、真实后缀、下载带 `Referer`、`master1200`）、
  facebook `watch/?v=` 与 `v` 参数保留、bilibili `view/detail` 需 cookie、
  yt-dlp 条目缺 `thumbnail`/`description` 时不再 KeyError、保留原语言音轨、
  **youtube 社区帖子**（一个 parser 管视频 + 帖子，帖子读页面 `ytInitialData`）、
  **facebook / snapchat / youtube 视频改自研**（不再调用 yt-dlp）。
- **facebook / snapchat / youtube 视频不再走 yt-dlp**（2026-10-08）：
  - facebook 读页面 `data-sjs` 里的明文渐进式直链（`videoDeliveryLegacyFields.browser_native_hd_url`，
    三种页面形态 watch/post/reel **不同构**）；DASH-only 明确抛错、不半吊子解析 MPD。
  - snapchat 读 spotlight 页 `__NEXT_DATA__` 的 `contentUrl`（明文）；它的
    `videoMetadata.description` 是**固定模板**，不当正文。
  - youtube 走 innertube player + **`VISIONOS` client**（`provider_api/youtube_video.py`）。
    **选型判据：既要返回明文 url、又不要 PO token** —— `ANDROID` 只给 1 条 360p 合一档、
    `ANDROID_VR` 给全 27 条但直链**必然 403**（`GVS_PO_TOKEN_POLICY: required=True`）、
    `IOS` 一条不给。高画质是分离流 ⇒ ffmpeg `-c copy` mux。
    **别把 `ANDROID_VR` 加回 CLIENTS 当兜底**：player 请求"成功"会让它永远挡住兜底，
    症状是"解析全对、下载全 403"。
  - ⚠️ **player 请求必须带 `X-Goog-Visitor-Id`**（2026-10-08）：缺它时 VISIONOS 会返回
    `LOGIN_REQUIRED`（用户报「YouTube 解析失败」的真因，**不是代理问题**）。visitorData 取
    `youtubei/v1/guide` 的 `responseContext.visitorData`（**别抓 `/watch` 页 HTML** ——
    实测会 429 限流，一失败就退化）。它是访客身份、可跨视频复用 ⇒ 进程内缓存 1 小时。
    **且 代理与这个头缺一不可**：161 直连时带上它仍被拦（WARP 必须保留）。
    详见 `doc/2026-10-08-youtube-visitor-id.md`。

- **youtube 帖子的正文链接要还原**（2026-10-08）：
  YouTube 会把帖子正文里 run 的**显示文本**截断成 `https://…list...`（页面源码里就是字面的省略号），
  但完整地址在同一条 run 的 `navigationEndpoint` 上，三种载体：`urlEndpoint.url`（外链，常包一层
  `youtube.com/redirect?…&q=`）、`commandMetadata.webCommandMetadata.url`、
  `browseEndpoint.canonicalBaseUrl`（站内路径）。`_text_of()` 现在遇到**被截断**的 URL 形态 run
  就产出 `<a href="完整地址">原显示文本</a>` —— **保留省略号原文**，只让链接指向真实目标
  （解 redirect、相对路径补域名）；`#hashtag` 不受影响。未截断的链接一个字节都不动。
  ⚠️ **三个载体都可能是相对路径**，只在其中一级取会得到 `/playlist?list=…` 这种半截链接。

- **yt-dlp 已彻底移除**（2026-10-08，`feat!: drop yt-dlp entirely`）：
  - bilibili 不再有 yt-dlp 兜底（原来 `BiliYtParse` 在 API 失败时用它），只走自身 API；
    失败时**带上真实原因**（原实现吞成一句「Bilibili 解析失败」，看不出是风控 / cookie / 接口变更）。
  - 连带删除：`parsers/base/ytdlp.py`、`provider_api/ytdlp.py`、依赖 `yt-dlp[default]`
    （及 yt-dlp-ejs / brotli / brotlicffi / mutagen / websockets 这些传递依赖）、
    **Dockerfile 里的 deno**（只为 yt-dlp 解 nsig 装的）。**ffmpeg 保留**（YouTube 1080p 要 mux）。
  - `types/serialize.py` 的「构造不了的结果类不缓存」机制**保留**（防的是任何需要运行期句柄的类，
    不只是原来那两个 yt-dlp 类），测试改用自建类钉住。
  - ⚠️ B 站 `view/detail` **对匿名请求直接风控**，端到端验证必须走 `ParseService`（会带 cookie）；
    裸用 `ParseHub().parse(url)` 会稳定 412，别误判成解析器坏了。密度过高也会 412（冷却后恢复）。

- **图片扩展名一律按 URL 取**（2026-10-08）：`ImageRef.ext` 默认 `jpg`，各 parser 构造时普遍
  没传 ⇒ 图片实为 PNG/WebP 也被命名成 `.jpg`，下游按后缀处理媒体会炸（见 §11）。
  现在**统一**走 `utils/helpers.image_ext_from_url()`（path 扩展名 → query `format=` → default），
  16 个平台全部接入，pixiv 自己那份重复的 `_guess_ext` 已删除并入。
  动图（`AniRef`）传 `ANIMATED_EXTS`（twitter 的动图 URL 是 mp4）。
  **纯增量**：URL 给不出可信扩展名时返回 default，行为与"不传 ext"一致。
  详见 `doc/2026-10-08-unify-image-ext.md`（含真机验证结果与**未验证平台清单**）。

## 8. 平台配置的容错（本地修改）

`core/platform_config.py`，两处与原版不同：

- 列表字段（`cookies` / `parser_proxies` / `downloader_proxies`）里的**空条目归一化为 `None`**。
  删掉 cookie 值只留一个 `-` 是常见写法，YAML 解析成 `[None]` —— 留空应视为"该平台退化为匿名"，
  而不是配置错误（2026-10-04 用户就这么把 bot 搞下线过一次）。
- 单个平台**校验失败或平台名不存在**时，跳过该平台并记 `error` 日志，**不再 `raise SystemExit(1)`**。
  服务着 22 个平台，一处笔误不该让整体下线。真错误（未知字段 / 类型不对）仍会被拦下。

## 9. 静态检查（本地新增）

`scripts/check.sh` + `.pylintrc`：提交前跑 ruff **加** pylint `--errors-only`。

- **pylint 是必需的** —— ruff 抓不到"名字导错模块"，而**函数体内的延迟 import** 在模块加载时
  不报错，只在该分支执行时炸（2026-10-04 因此漏了一个生产 ImportError）。
- `.pylintrc` 只关框架动态属性的误报（SQLAlchemy `func.now()`、pydantic `model_fields` 等），
  E0611/E1123/E0701/E0704 这些**抓真问题的码一律保留**。
- 合上游时**保留这两个文件**，别被上游的同名文件覆盖。

## 10. i18n

新增文案（「查看」、「上 传 中...」等）补齐 16 种语言：de / en / es /
fr / id / it / ja / ko / nl / pl / pt-br / ru / th / tr / vi / zh-hant。

## 11. 媒体处理：保存格式跟**真实格式**走，不跟后缀走

`utils/media_processing_unit.py`。Telegram 兼容性处理（填充 / 切割 / 降采样）里，
**只有降采样曾按文件后缀猜编码器**：`resized.save(out_path)` 不传 `format=` ⇒ Pillow 用后缀选
编码器 ⇒ 后缀撒谎的调色板图（PNG 内容 + `.jpg` 名，mode `P`）编不了 JPEG，抛
`cannot write mode P as JPEG`，**整条媒体处理失败、用户什么都收不到**（2026-10-08 报障）。

现在保存格式与输出后缀都跟 Pillow 检测到的真实格式走（`_PIL_FORMAT_SUFFIX` 做格式名 → 后缀映射），
真 JPEG 走原路径零变化。填充 / 切割 / 取色三条路径实测对 `P` 模式本来就安全。
⚠️ `process_image` 的 `needs_rgb = image_mode == "RGBA"` **只管 RGBA**，别以为它兜住了所有模式。

## 11.1 下载器整段路径也必须带 Range 头（2026-10-08）

`utils/downloader.py::_download_single` 原本**不带** `Range` 头。googlevideo 的部分直链
query 里有 **`rqh=1`**（要求 Range 头）：不带 Range 的整段请求会被**直接断连**
（`curl: (56) Connection closed abruptly`，稳定复现），而 `Range: bytes=0-` 正常返回 206 整段。

⇒ 单连接路径统一带 `Range: bytes=0-`（对支持 Range 的服务器等价整段、对不支持者会被忽略）。
症状很有迷惑性：**>10MB 的流能下（走分片）、小流必挂**，看起来像"网络不稳"。

**上游同步注意**：`lib/` 是 subtree，合上游时这一行可能被覆盖掉。

## 上游同步

```bash
# bot 上游
git fetch upstream && git merge upstream/main
# 库上游 (改的是 lib/ 子树)
git subtree pull --prefix=lib https://github.com/z-mio/ParseHub.git master
```

## Credits

- [ParseHubBot (z-mio/parse_hub_bot)][upstream-bot] — bot 部分的基础
- [ParseHub (z-mio/ParseHub)][upstream-lib] — 解析库（现位于 `lib/`）

## License

MIT License，见 [LICENSE](LICENSE)（库见 `lib/LICENSE`）。

[upstream-bot]: https://github.com/z-mio/parse_hub_bot
[upstream-lib]: https://github.com/z-mio/ParseHub
