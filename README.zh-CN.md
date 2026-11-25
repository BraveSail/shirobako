<div align="center">

# 🎬 shirobako

**Telegram 多平台聚合解析机器人**

本项目基于 [z-mio/parse_hub_bot](https://github.com/z-mio/parse_hub_bot) 与
[z-mio/ParseHub](https://github.com/z-mio/ParseHub) 改造，已独立维护（不是任何仓库的 fork）。
解析库以 **git subtree** 内置于 `lib/`，单仓库即可构建、测试、部署。
本地改动清单见 [LOCAL_FORK_MODIFICATIONS.md](LOCAL_FORK_MODIFICATIONS.md)。

<p align="center">
  <a href="https://github.com/BraveSail/shirobako/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/BraveSail/shirobako?style=flat-square&color=5D6D7E" alt="License">
  </a>
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/Python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  </a>
  <a href="https://t.me/ParseHubot">
    <img src="https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=flat-square&logo=telegram&logoColor=white" alt="Telegram Bot">
  </a>
  <a href="https://github.com/astral-sh/uv">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=flat-square" alt="uv">
  </a>
</p>

简体中文 | [English](README.md)

[**🤖 上游实例**](https://t.me/ParseHubot) ·
[**📚 解析库 (lib/)**](https://github.com/BraveSail/shirobako/tree/main/lib) ·
[**🐛 问题反馈**](https://github.com/BraveSail/shirobako/issues)

</div>

---

> 上游官方实例：[@ParseHubot](https://t.me/ParseHubot)（本项目是独立的自托管版本）

## ✨ 功能特性

- 🎬 **多平台解析** — 抖音、B站、YouTube、小红书、Twitter 等 22 个平台一站搞定
- 📰 **富文本排版** — 正文按 Telegram 富文本消息还原原文格式, 发布时间/浏览量/来源收在页尾
- ⚡ **内联模式** — 在任意聊天窗口输入 `@BotUsername <链接>` 即可解析
- 🖼️ **Tg 兼容** — 自动转码、长图切割、大视频分段
- 📦 **多种模式** — 在线预览, 原始文件, 打包下载
- 🐳 **Docker 部署** — 开箱即用

## 📦 支持平台一览

| 平台            | 视频 | 图文 |           其他            |
|:----------------|:----:|:----:|:-------------------------:|
| **Twitter / X** |  ✅  |  ✅  |          📝 文章          |
| **Instagram**   |  ✅  |  ✅  |                           |
| **YouTube**     |  ✅  |      |          🎵 音乐          |
| **Facebook**    |  ✅  |      |                           |
| **Threads**     |  ✅  |  ✅  |                           |
| **Pixiv**       |      |  ✅  |          🖼️ 插画          |
| **Bilibili**    |  ✅  |      |          📝 动态          |
| **linux.do**    |      |  ✅  |          📝 论坛          |
| **抖音**        |  ✅  |  ✅  |          ☀️日常           |
| **TikTok**      |  ✅  |  ✅  |                           |
| **微博**        |  ✅  |  ✅  |                           |
| **小红书**      |  ✅  |  ✅  |                           |
| **贴吧**        |  ✅  |  ✅  |                           |
| **微信公众号**  |      |  ✅  |                           |
| **快手**        |  ✅  |  ✅  |                           |
| **酷安**        |      |  ✅  |                           |
| **皮皮虾**      |  ✅  |  ✅  |                           |
| **最右**        |  ✅  |  ✅  |                           |
| **小黑盒**      |  ✅  |  ✅  |                           |
| **Snapchat**    |  ✅  |      |                           |
| **知乎**        |  ✅  |  ✅  | 🐶 问答, 专栏, 圈子, 日报 |
| **豆瓣**        |  ✅  |  ✅  |        👥 小组话题        |

> 🔧 更多平台持续接入中...

## 🚀 快速开始

### 🐳 Docker 运行 (推荐)

在可访问 Redis 的 Linux 宿主机上（默认 `redis://127.0.0.1:6379/0`），
任选 GHCR 镜像或本地构建。两份 Compose 使用相同的项目名、宿主机网络及
`./data` / `./logs` 绑定挂载，不要分别启动两套实例。

```bash
git clone https://github.com/BraveSail/shirobako.git
cd shirobako
cp .env.example .env   # 仅首次安装；至少填 API_ID / API_HASH / BOT_TOKEN
```

**方式 A — 拉取 GHCR 预构建镜像**（首次成功发布后可用）：

```bash
docker compose -f docker-compose.yaml pull bot
docker compose -f docker-compose.yaml up -d --no-build bot
```

一个镜像索引同时包含 `linux/amd64` 和 `linux/arm64`，Compose 自动选择宿主架构。
要固定部署版本，在 `.env` 中设置完整镜像引用，例如
`SHIROBAKO_IMAGE=ghcr.io/bravesail/shirobako:v1.2.3` 或
`ghcr.io/bravesail/shirobako@sha256:<索引摘要>`。首次创建的包可能是私有的，
需要匿名拉取时可在 GHCR 包设置中改为公开。

**方式 B — 本地构建**（首次镜像发布前同样可用）：

```bash
docker compose -f compose.deploy.yaml build bot
docker compose -f compose.deploy.yaml up -d bot
```

CI 和本地生产构建统一使用 `Dockerfile.deploy`，包含 Bun 与 SABR 依赖。
现有 `compose.deploy.yaml` 部署可继续使用 `scripts/deploy_remote.sh`。
如曾使用旧版上游 `docker-compose.yaml`，先备份命名卷数据并迁移到 `./data` /
`./logs`，再停止旧项目并切换。

#### 镜像发布

| 事件 | 结果 |
|---|---|
| 推送到 `main` | 两个原生 runner 各自用 `Dockerfile.deploy` 构建镜像，运行默认启动命令，连接隔离的测试端点，检查 CPU 架构、SQLite 迁移、FFmpeg、Bun 和 SABR。两边通过后，把验过的镜像组成一个索引，首次写入 `sha-<完整提交 SHA>`。 |
| 手动运行 **Docker Image CI** | 默认只做两种架构的检查。在 `main` 勾选 `publish` 后按分支推送的规则发布。 |
| 发布 GitHub Release（含预发布） | 将该提交已核验的索引 digest 加上原始版本标签。缺少已核验索引，或版本已指向其他 digest，发布任务失败。仅推送 Git 标签不会触发发布。 |

SHA 标签只写一次。版本标签允许 1–128 个 ASCII 字母、数字、下划线、点或连字符，
首字符为字母、数字或下划线。`main`、`latest` 和 `sha-` 前缀已保留。
只有发布任务使用内置 `GITHUB_TOKEN` 的 `packages: write` 权限。
请为本仓库授予包的 Actions 写权限，并让此工作流独占这些标签的写入权。这些规则从使用新版工作流的运行开始生效。
如果旧 SHA 标签缺少双架构验证标记，请选择新验证的提交发布版本。

`main` 与 `latest` 在同一把发布锁内沿各自已发布提交的历史向前移动。候选提交须为锁内读取的
main 顶端或其祖先，两者之间的最终差异仅可涉及 `README.md`、`README.zh-CN.md`、
`LOCAL_FORK_MODIFICATIONS.md` 和 `doc/` 下的 Markdown。因此，构建期间即使追加这些路径的
`[skip ci]` 文档提交，也可把两个标签推进到实际验过的代码提交。SHA 标签始终对应实际构建的源码。
若跳过的改动超出上述范围，或整次推送都被跳过、没有启动构建，可在当前 main 手动发布。重写历史后须先核对现有标签指向。

GitHub 可保留最多 100 个等待中的发布任务。队列满或两个标签之间的写入被打断时，可重跑目标发布；
若三天后构建产物已过期，重跑全部任务。离线探针验证到机器人发起代理握手为止，上线后的 Telegram
就绪检查仍由 `scripts/wait_ready.py` 完成。

### 💻 源码运行

```bash
uv sync          # uv workspace 会把 lib/ 里的解析库一并装成可编辑依赖
uv run bot.py
```

### 🧪 测试

```bash
uv run pytest test/                       # bot
uv run --package parsehub pytest lib/test/  # 解析库
```

### 🔄 同步上游

```bash
git fetch upstream && git merge upstream/main                      # bot 上游
git subtree pull --prefix=lib https://github.com/z-mio/ParseHub.git master   # 库上游
```

---

## ⚙️ 配置说明

- **环境变量:** 基础配置
- **平台配置 (可选):** 平台代理和 Cookie

### 📝 环境变量

```dotenv
# ✅ 必填
API_ID=        # Telegram API ID，登录 https://my.telegram.org 获取
API_HASH=      # Telegram API Hash，同上获取
BOT_TOKEN=     # 机器人 Token，向 @BotFather 申请

# 🔲 可选
BOT_PROXY=     # Bot 连接 Telegram 的代理, 如 http://127.0.0.1:7890

# 只允许**一个群**的成员使用 inline 与 guest 查询 (0 = 不限制)
GUEST_WHITELIST_GROUP_ID=0
```

### 🔒 访问控制（inline 与 guest 查询）

inline（`@你的bot 链接`）和 guest（在 bot 未加入的群里 @它）都会把用户输入送进解析链路，
所以加了门禁：

- 把 `GUEST_WHITELIST_GROUP_ID` 设为一个 bot 所在的群，则**只有该群成员**能用 inline / guest。
- 判定只查这一个群（一次 `getChatMember`），并按用户缓存（放行 10 分钟、拒绝 1 分钟）
  —— 不遍历 bot 所在的所有群，群再多也不会 flood。
- 私聊里的 inline 不限制：用户是主动来找 bot 的。
- `0`（默认）表示完全不限制。

guest 模式本身要在 @BotFather 里开启（Bot Settings → Guest Mode）；bot 用
`answer_guest_query` 把结果直接贴进该群。

### 🌐 平台配置

用于为各解析平台单独配置 **代理**和 **Cookie**，位于 `data/config/platform_config.yaml`

```yaml
# ═══════════════════════ 全局默认代理 ═══════════════════════
# 当某平台未单独配置代理时，会使用全局默认代理
# 支持填写单个地址(字符串)或多个地址(列表，随机选取)
# 支持 http://、https://、socks5://、socks5h:// 协议

default_parser_proxies: http://127.0.0.1:7890        # 解析代理（单个）
default_downloader_proxies: # 下载代理（代理池）
  - http://127.0.0.1:7890
  - http://127.0.0.1:7891

# ═══════════════════════ 平台独立配置 ═══════════════════════
platforms:
  <platform_id>: # 平台 ID，见下方支持列表
    disable_parser_proxy: false          # 是否禁用解析代理（直连）
    disable_downloader_proxy: false      # 是否禁用下载代理（直连）
    parser_proxies: # 该平台专用解析代理池
      - http://proxy1:port
    downloader_proxies: # 该平台专用下载代理池
      - http://proxy2:port
    cookies: # 该平台 Cookie 列表（随机选取）
      - "cookie_string_1"
      - "cookie_string_2"
```

### 🔀 代理优先级

解析代理和下载代理各自遵循相同的优先级逻辑：

```
禁用代理 (disable_*_proxy: true)
  ↓ 未禁用
平台专用代理 (parser_proxies / downloader_proxies)
  ↓ 未配置
全局默认代理 (default_parser_proxies / default_downloader_proxies)
  ↓ 未配置
直连（不使用代理）
```

> 💡 当代理池中有多个地址时，每次请求会 **随机选取**一个

### 🔑 支持的平台 ID

`<platform_id>` 必须是以下合法的平台 ID：

| 平台 ID     | 对应平台    |
|:------------|:------------|
| `twitter`   | Twitter / X |
| `instagram` | Instagram   |
| `youtube`   | YouTube     |
| `facebook`  | Facebook    |
| `threads`   | Threads     |
| `pixiv`     | Pixiv       |
| `bilibili`  | 哔哩哔哩    |
| `linuxdo`   | linux.do    |
| `douyin`    | 抖音        |
| `tiktok`    | TikTok      |
| `weibo`     | 微博        |
| `xhs`       | 小红书      |
| `tieba`     | 百度贴吧    |
| `weixin`    | 微信公众号  |
| `kuaishou`  | 快手        |
| `coolapk`   | 酷安        |
| `pipix`     | 皮皮虾      |
| `zuiyou`    | 最右        |
| `xiaoheihe` | 小黑盒      |
| `snapchat`  | Snapchat    |
| `zhihu`     | 知乎        |
| `douban`    | 豆瓣        |

### 🍪 支持 Cookie 的平台

- `Twitter / X`
- `Instagram`
- `Threads`
- `YouTube`
- `Pixiv`
- `Bilibili`
- `linux.do`
- `抖音`
- `TikTok`
- `快手`
- `小红书`
- `知乎`
- `豆瓣`

### 📌 配置示例

##### 示例 1：国内平台直连，海外平台走代理

```yaml
default_parser_proxies: http://127.0.0.1:7890
default_downloader_proxies: http://127.0.0.1:7890

platforms:
  bilibili:
    disable_parser_proxy: true
    disable_downloader_proxy: true
  douyin:
    disable_parser_proxy: true
    disable_downloader_proxy: true
  xhs:
    disable_parser_proxy: true
    disable_downloader_proxy: true
```

#### 示例 2：Twitter 配置 Cookie + 使用全局代理

```yaml
default_parser_proxies: http://127.0.0.1:7890
default_downloader_proxies: http://127.0.0.1:7890

platforms:
  twitter:
    cookies:
      - "auth_token=your_token_here; ct0=your_ct0_here"
```

#### 示例 3：YouTube 使用独立代理池

```yaml
platforms:
  youtube:
    parser_proxies:
      - http://proxy-us-1:8080
      - http://proxy-us-2:8080
      - http://proxy-eu-1:8080
    downloader_proxies:
      - http://proxy-us-1:8080
      - http://proxy-eu-1:8080
```

#### 示例 4：B站指定 Cookie 轮换 + 解析直连 + 下载走代理

```yaml
platforms:
  bilibili:
    disable_parser_proxy: true
    downloader_proxies:
      - http://127.0.0.1:7890
    cookies:
      - "SESSDATA=xxx; bili_jct=xxx; buvid3=xxx"
      - "SESSDATA=yyy; bili_jct=yyy; buvid3=yyy"
```

## 🤝 参与贡献

欢迎提交 Pull Request 或 Issue!

- 核心解析相关请前往 [ParseHub](https://github.com/z-mio/ParseHub)
- Bug 反馈请附上相关 URL 和日志信息

### 开发规范

提交代码前请至少执行:

```bash
ruff format && ruff check --fix && uv run mypy
uv run pytest
```

## 🙏 致谢

- [ParseHubBot (z-mio/parse_hub_bot)](https://github.com/z-mio/parse_hub_bot) — bot 部分的基础
- [ParseHub (z-mio/ParseHub)](https://github.com/z-mio/ParseHub) — 解析库（现位于 `lib/`）

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 协议开源（沿用上游协议）

---

<div align="center">

**如果这个项目对你有帮助，欢迎点个 ⭐ Star!**

</div>

