#!/usr/bin/env bash
# YouTube cookie: 无头刷新 -> 写入 bot 配置（原子 + 校验）。
#
# 由 yt-cookie-refresh.service 调用（timer 每 6h）。失败退非零，
# systemd 里能看到 —— 不要静默继续用旧 cookie。
#
# 前置依赖（宿主机，不在容器里）:
#   - Playwright + Chromium: /opt/yt-browser/bin/python
#   - 仓库 checkou: /root/parse_hub_bot（用它的 .venv 跑 set_youtube_cookie.py）
set -uo pipefail

REPO="${REPO:-/root/parse_hub_bot}"
PY="${PY:-/opt/yt-browser/bin/python}"
LOG="/root/yt-cookie/cron.log"

ts() { date "+%Y-%m-%dT%H:%M:%S"; }

echo "$(ts) === 开始刷新 ===" >> "$LOG"

# 1) 无头刷新（Playwright 让 Google 滚动续期）
if ! timeout 300 "$PY" "$REPO/scripts/yt-cookie/refresh_cookie.py" >> "$LOG" 2>&1; then
    echo "$(ts) !! 无头刷新失败（cookie 可能已被踢下线，需要人工提供新 cookie）" >> "$LOG"
    exit 1
fi

# 2) 写进 bot 配置并校验
if ! timeout 120 "$PY" "$REPO/scripts/yt-cookie/push_cookie_to_bot.py" >> "$LOG" 2>&1; then
    echo "$(ts) !! 推送配置失败" >> "$LOG"
    exit 1
fi

echo "$(ts) === 完成 ===" >> "$LOG"
