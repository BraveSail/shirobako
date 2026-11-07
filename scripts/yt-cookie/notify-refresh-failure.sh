#!/usr/bin/env bash
# 刷新失败的家用告警 —— 用 bot 自己的 token 直接发 Telegram。
#
# 为什么需要它: 2026-10-23 之后刷新连续失败 60 次、两周无人知晓，
# 因为失败只写进 journalctl 和 cron.log —— 没人会主动去看。
# 「日志里写清楚」不等于「有人知道」。
#
# 用法: notify-refresh-failure.sh <原因>
set -uo pipefail

REPO="${REPO:-/root/parse_hub_bot}"

# ★ 用 source 读 .env，不要 grep|cut：
#   grep|cut 那套在本机实测拿到的是空值（引号/行尾处理差异），
#   结果 curl 打到 /bot/getMe 是 401，而脚本静默退出 0 —— 告警等同于没发。
#   source 是 bash 原生解析，与 bot 自己读 .env 的方式一致。
# shellcheck disable=SC1091
set -a
. "$REPO/.env" 2>/dev/null
set +a

TOKEN="${BOT_TOKEN:-}"
ADMINS="${ADMIN_USERS:-}"
REASON="${1:-未知原因}"

if [ -z "$TOKEN" ] || [ -z "$ADMINS" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) !! 告警发不出去: 缺 BOT_TOKEN 或 ADMIN_USERS" >> /root/yt-cookie/cron.log
    exit 0
fi

# ADMIN_USERS 可能是 "123" 或 "123,456"
FIRST=$(echo "$ADMINS" | cut -d, -f1 | tr -d ' ')

TEXT="⚠️ YouTube cookie 刷新失败

原因: ${REASON}

影响: 新视频的 SABR 取流会失败（attestation rejected）。

需要: 人工提供一份新的 YouTube cookie。
日志: /root/yt-cookie/cron.log"

resp=$(curl -sS --max-time 20 \
    -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${FIRST}" \
    --data-urlencode "text=${TEXT}" 2>&1)

# ★ 结果必须验证并落日志 —— 静默失败等于没告警
case "$resp" in
    *'"ok":true'*)
        echo "$(date +%Y-%m-%dT%H:%M:%S) 告警已发送到 $FIRST" >> /root/yt-cookie/cron.log
        ;;
    *)
        echo "$(date +%Y-%m-%dT%H:%M:%S) !! 告警发送失败: ${resp:0:200}" >> /root/yt-cookie/cron.log
        ;;
esac
