<div align="center">

# 🎬 shirobako

**Telegram Multi-Platform Content Parsing Bot**

Built on [z-mio/parse_hub_bot](https://github.com/z-mio/parse_hub_bot) and
[z-mio/ParseHub](https://github.com/z-mio/ParseHub), and maintained independently — this is
not a fork of either. The parser library is vendored under `lib/` (git subtree), so one clone
builds, tests and deploys everything. See
[LOCAL_FORK_MODIFICATIONS.md](LOCAL_FORK_MODIFICATIONS.md) for the list of local changes.

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

[简体中文](README.zh-CN.md) | English

[**🤖 Upstream Demo**](https://t.me/ParseHubot) ·
[**📚 Parser Library (lib/)**](https://github.com/BraveSail/shirobako/tree/main/lib) ·
[**🐛 Report an Issue**](https://github.com/BraveSail/shirobako/issues)

</div>

---

> Upstream official bot: [@ParseHubot](https://t.me/ParseHubot) (this repository is an independent, self-hosted build)

## ✨ Features

- 🎬 **Multi-platform parsing** — Parse content from 22 major platforms, including Douyin, Bilibili, YouTube,
  Xiaohongshu, and Twitter
- ⚡ **Inline mode** — Parse a link from any chat by typing `@BotUsername <link>`
- 🖼️ **Telegram-ready media** — Automatic transcoding, long-image splitting, and large-video segmentation
- 📰 **Rich formatting** — The body is sent as a Telegram rich message that keeps the original
  layout, with publish time, view count and source in the footer
- 📦 **Multiple delivery modes** — Online preview, original file, or packaged download
- 🐳 **Docker deployment** — Ready to use out of the box

## 📦 Supported Platforms

| Platform        | Video | Image Posts |              Other              |
|:----------------|:-----:|:-----------:|:-------------------------------:|
| **Twitter / X** |  ✅   |     ✅      |           📝 Articles           |
| **Instagram**   |  ✅   |     ✅      |                                 |
| **YouTube**     |  ✅   |             |            🎵 Music             |
| **Facebook**    |  ✅   |             |                                 |
| **Threads**     |  ✅   |     ✅      |                                 |
| **Pixiv**       |       |     ✅      |          🖼️ Illustrations        |
| **Bilibili**    |  ✅   |             |           📝 Updates            |
| **linux.do**    |       |     ✅      |            📝 Forum             |
| **Douyin**      |  ✅   |     ✅      |         ☀️ Daily posts          |
| **TikTok**      |  ✅   |     ✅      |                                 |
| **Weibo**       |  ✅   |     ✅      |                                 |
| **Xiaohongshu** |  ✅   |     ✅      |                                 |
| **Tieba**       |  ✅   |     ✅      |                                 |
| **WeChat OA**   |       |     ✅      |                                 |
| **Kuaishou**    |  ✅   |     ✅      |                                 |
| **Coolapk**     |       |     ✅      |                                 |
| **Pipixia**     |  ✅   |     ✅      |                                 |
| **Zuiyou**      |  ✅   |     ✅      |                                 |
| **Xiaoheihe**   |  ✅   |     ✅      |                                 |
| **Snapchat**    |  ✅   |             |                                 |
| **Zhihu**       |  ✅   |     ✅      | 🐶 Q&A, columns, circles, Daily |
| **Douban**      |  ✅   |     ✅      |         👥 Group topics         |

> 🔧 More platforms are being added continuously...

## 🚀 Quick Start

### 🐳 Run with Docker (recommended)

On a Linux host with Redis available (default: `redis://127.0.0.1:6379/0`),
use either the published image or a local build. Both Compose files use host networking,
the same project name and `./data` / `./logs` bind mounts. Do not start both as separate deployments.

```bash
git clone https://github.com/BraveSail/shirobako.git
cd shirobako
cp .env.example .env   # new installs only; fill API_ID / API_HASH / BOT_TOKEN
```

**Option A — prebuilt GHCR image** (available after the first successful publication):

```bash
docker compose -f docker-compose.yaml pull bot
docker compose -f docker-compose.yaml up -d --no-build bot
```

Compose selects the host platform from one image index supporting `linux/amd64`
and `linux/arm64`. To pin a deployment, set `SHIROBAKO_IMAGE` in `.env` to a
full image reference such as `ghcr.io/bravesail/shirobako:v1.2.3` or
`ghcr.io/bravesail/shirobako@sha256:<index digest>`. The first package may be
private. Enable public visibility in GHCR if anonymous pulls are needed.

**Option B — build locally** (also works before the first image is published):

```bash
docker compose -f compose.deploy.yaml build bot
docker compose -f compose.deploy.yaml up -d bot
```

Both CI and local production builds use `Dockerfile.deploy`, including Bun and SABR.
Existing `compose.deploy.yaml` deployments can keep using `scripts/deploy_remote.sh`.
If migrating from the old upstream `docker-compose.yaml`, back up and migrate any
existing named-volume data to `./data` / `./logs`, and stop the old project first.

#### Image publication

| Event | Result |
|---|---|
| Push to `main` | Each native runner builds `Dockerfile.deploy`, boots the default command against an isolated test endpoint and checks the CPU, SQLite migrations, FFmpeg, Bun and SABR. After both pass, the writer assembles their tested images into one index tagged `sha-<full commit SHA>`. |
| Manual **Docker Image CI** | The default runs both checks. Enable `publish` on `main` for the same publication as a push. |
| Published GitHub Release (including a prerelease) | Applies the exact release tag to the verified index digest already published for the tagged commit. A missing verified index or a different digest under that version fails the release job. A bare Git tag push has no publish job. |

SHA tags are write-once. Version tags accept 1–128 ASCII letters, digits,
underscores, dots and hyphens, starting with a letter, digit or underscore.
`main`, `latest` and the `sha-` prefix are reserved. Only the publish job gets
`packages: write` with the built-in `GITHUB_TOKEN`. Give this repository Actions
access to the package and keep this workflow its sole tag writer. These rules
apply to runs using this workflow revision. A legacy SHA tag without the
two-platform verification marker requires a new verified commit for release.

`main` and `latest` advance from their published commits under a single writer
lock. A candidate must reach the current main tip by ancestry, with net changes
limited to `README.md`, `README.zh-CN.md`, `LOCAL_FORK_MODIFICATIONS.md` and
Markdown under `doc/`. Thus a newer documentation commit with `[skip ci]` can
leave both aliases on the verified code commit, even if it landed during the
build. The SHA label identifies the commit actually built. For a skipped change
outside those paths, or if CI skipped the whole push and no build ran, run a
manual publish on current main. Rewritten history
requires checking the existing aliases before advancing them.

GitHub queues up to 100 waiting writers. If the queue fills or one alias write
is interrupted, rerun the intended publication. If its three-day artifacts
have expired, rerun all jobs. The offline probe covers startup through the
first proxy handshake. Deployment readiness still uses `scripts/wait_ready.py`
with a real Telegram connection.

### 💻 Run from Source

```bash
uv sync          # the uv workspace installs lib/ as an editable dependency
uv run bot.py
```

### 🧪 Tests

```bash
uv run pytest test/                        # bot
uv run --package parsehub pytest lib/test/ # parser library
```

### 🔄 Sync upstream

```bash
git fetch upstream && git merge upstream/main                                 # bot upstream
git subtree pull --prefix=lib https://github.com/z-mio/ParseHub.git master    # library upstream
```

---

## ⚙️ Configuration

- **Environment variables:** Required base configuration
- **Platform configuration (optional):** Per-platform proxies and cookies

### 📝 Environment Variables

```dotenv
# ✅ Required
API_ID=        # Telegram API ID; obtain it at https://my.telegram.org
API_HASH=      # Telegram API Hash; obtain it at the same place
BOT_TOKEN=     # Bot token; create one via @BotFather

# 🔲 Optional
BOT_PROXY=     # Proxy for the bot's Telegram connection, e.g. http://127.0.0.1:7890

# Restrict inline and guest queries to members of ONE group (0 = no restriction).
# The bot checks membership in this group only, so more groups never mean more API calls.
GUEST_WHITELIST_GROUP_ID=0
```

### 🔒 Access control (inline & guest queries)

Inline queries (`@yourbot <link>`) and guest queries (mentioning the bot in a group it
has not joined) both pipe user input into the parser, so they are gated:

- Set `GUEST_WHITELIST_GROUP_ID` to a group the bot is in. Only **members of that group**
  may then use inline or guest queries.
- The check is a single `getChatMember` call against that one group, cached per user
  (10 minutes when allowed, 1 minute when denied) — it does not scan every group the bot
  belongs to, so the API is never flooded no matter how many groups the bot is in.
- Private-chat inline queries stay open: the user came to the bot on purpose.
- `0` (the default) disables the gate entirely.

Guest mode itself must be enabled in @BotFather (Bot Settings → Guest Mode); the bot
posts its reply straight into the group via `answer_guest_query`.

### 🌐 Platform Configuration

Configure **proxies** and **cookies** for each parser platform in `data/config/platform_config.yaml`.

```yaml
# ═══════════════════════ Global default proxies ═══════════════════════
# A platform without an individual proxy configuration uses the global default.
# A proxy may be a single address (string) or a pool of addresses (list, selected at random).
# Supported schemes: http://, https://, socks5://, socks5h://

default_parser_proxies: http://127.0.0.1:7890        # Parser proxy (single)
default_downloader_proxies: # Downloader proxy (pool)
  - http://127.0.0.1:7890
  - http://127.0.0.1:7891

# ═══════════════════════ Per-platform configuration ═══════════════════════
platforms:
  <platform_id>: # Platform ID; see the supported-platform list below
    disable_parser_proxy: false          # Disable parser proxy (use direct connection)
    disable_downloader_proxy: false      # Disable downloader proxy (use direct connection)
    parser_proxies: # Dedicated parser proxy pool for this platform
      - http://proxy1:port
    downloader_proxies: # Dedicated downloader proxy pool for this platform
      - http://proxy2:port
    cookies: # Cookie list for this platform (selected at random)
      - "cookie_string_1"
      - "cookie_string_2"
```

### 🔀 Proxy Priority

Parser and downloader proxies each use the same priority order:

```
Disable proxy (disable_*_proxy: true)
  ↓ if not disabled
Platform-specific proxy (parser_proxies / downloader_proxies)
  ↓ if not configured
Global default proxy (default_parser_proxies / default_downloader_proxies)
  ↓ if not configured
Direct connection (no proxy)
```

> 💡 When a proxy pool contains multiple addresses, one is **selected at random** for every request.

### 🔑 Supported Platform IDs

`<platform_id>` must be one of the following valid platform IDs:

| Platform ID | Platform    |
|:------------|:------------|
| `twitter`   | Twitter / X |
| `instagram` | Instagram   |
| `youtube`   | YouTube     |
| `facebook`  | Facebook    |
| `threads`   | Threads     |
| `pixiv`     | Pixiv       |
| `bilibili`  | Bilibili    |
| `linuxdo`   | linux.do    |
| `douyin`    | Douyin      |
| `tiktok`    | TikTok      |
| `weibo`     | Weibo       |
| `xhs`       | Xiaohongshu |
| `tieba`     | Baidu Tieba |
| `weixin`    | WeChat OA   |
| `kuaishou`  | Kuaishou    |
| `coolapk`   | Coolapk     |
| `pipix`     | 皮皮虾      |
| `zuiyou`    | Zuiyou      |
| `xiaoheihe` | Xiaoheihe   |
| `snapchat`  | Snapchat    |
| `zhihu`     | Zhihu       |
| `douban`    | Douban      |

### 🍪 Platforms Supporting Cookies

- `Twitter / X`
- `Instagram`
- `Threads`
- `YouTube`
- `Pixiv`
- `Bilibili`
- `linux.do`
- `Douyin`
- `TikTok`
- `Kuaishou`
- `Xiaohongshu`
- `Zhihu`
- `Douban`

### 📌 Configuration Examples

##### Example 1: Use direct connections for mainland Chinese platforms and a proxy for overseas platforms

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

#### Example 2: Configure a Twitter cookie and use the global proxy

```yaml
default_parser_proxies: http://127.0.0.1:7890
default_downloader_proxies: http://127.0.0.1:7890

platforms:
  twitter:
    cookies:
      - "auth_token=your_token_here; ct0=your_ct0_here"
```

#### Example 3: Use a dedicated proxy pool for YouTube

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

#### Example 4: Rotate Bilibili cookies, parse directly, and use a proxy for downloads

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

## 🤝 Contributing

Pull requests and issues are welcome!

- For core parsing features, please visit [ParseHub](https://github.com/z-mio/ParseHub).
- When reporting a bug, please include the relevant URL and log information.

### Development Guidelines

Before submitting code, run at least:

```bash
ruff format && ruff check --fix && uv run mypy
uv run pytest
```

## 🙏 Credits

- [ParseHubBot (z-mio/parse_hub_bot)](https://github.com/z-mio/parse_hub_bot) — the base of the bot
- [ParseHub (z-mio/ParseHub)](https://github.com/z-mio/ParseHub) — the parser library (now in `lib/`)

## 📄 License

This project is released under the [MIT License](LICENSE) (inherited from upstream).

---

<div align="center">

**If this project helps you, please consider giving it a ⭐ Star!**

</div>
