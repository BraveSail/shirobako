# yt-cookie —— YouTube 登录态 cookie 的自动刷新

`refresh_cookie.py` 是**宿主机侧**工具（**不进容器** —— 容器不装 Chromium）。

## 为什么需要它

YouTube 的反爬要求**登录 cookie**（`videoplayback` 直链与 watch 页取流都要）。
而 cookie 会过期，`__Secure-*SIDTS` 这类滚动令牌**只在真实页面请求里更新** ——
纯 HTTP 请求拿不到新值。所以用无头浏览器**带登录态访问一次 YouTube**，
让 Google 自己完成续期，再从浏览器导出全部 cookie。

## 何时刷新

`yt-cookie-refresh.timer`：**每 6 小时**（开机后 10 分钟首次；抖动 ±15min；`Persistent=true`）。

```bash
systemctl list-timers yt-cookie-refresh.timer     # 看下次触发
systemctl start yt-cookie-refresh.service         # 手动跑一次（与 timer 同路径）
systemctl show yt-cookie-refresh.service -p ExecMainStatus
```

## 链路

```
yt-cookie-refresh.timer
  └─ yt-cookie-refresh.service
       └─ refresh-and-push.sh
            ├─ refresh_cookie.py          Playwright 刷新 → /root/yt-cookie/cookies.json
            └─ push_cookie_to_bot.py      → set_youtube_cookie.py（原子写 + 回读校验 + 备份）
                 └─ platform_config.yaml  ──►  配置热重载（下轮解析生效，不用重启容器）
```

## 有效期怎么查

刷新脚本会记录浏览器给出的**真实 `expires`**（不是估算值）：

```bash
/opt/yt-browser/bin/python /root/parse_hub_bot/scripts/yt-cookie/refresh_cookie.py --check
# → cookie 条目 / 登录标志 / 上次刷新 / 关键项到期
```

## 能力边界（重要）

**只能续期，不能重新登录。** 种子 cookie 被踢下线时（改密码 / 安全策略变更）
`SID`/`HSID`/`SSID`/`__Secure-1PSID` 会缺失，脚本直接 `SystemExit` 拒绝继续 ——
此时**必须人工提供一份新 cookie**：

```bash
# 1) 浏览器登录 YouTube，导出 cookie 串写到服务器上的文件
# 2) 写进配置（会自动热重载）
python3 scripts/set_youtube_cookie.py /path/to/new_cookie.txt
```

无头浏览器做不到「给账号密码自己登」—— Google 有设备验证 / 2FA。

## 安装（新机器）

```bash
# 1) Playwright + Chromium（独立 venv，别塞进生产镜像）
python3 -m venv /opt/yt-browser && /opt/yt-browser/bin/pip install playwright
/opt/yt-browser/bin/playwright install chromium

# 2) 放入种子 cookie（浏览器导出，含 SID/HSID/SSID/__Secure-1PSID）
install -m 600 /dev/stdin /root/yt-cookie/cookies.json < seed.json

# 3) systemd
cp scripts/yt-cookie/systemd/*.service scripts/yt-cookie/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now yt-cookie-refresh.timer

# 4) 验证（判据取**容器内**读到的 cookie，不是文件时间戳）
bash scripts/yt-cookie/refresh-and-push.sh
```

## 日志

- `/root/yt-cookie/refresh.log` —— 刷新（含登录态断言、真实有效期）
- `/root/yt-cookie/cron.log` —— 完整链路（刷新 + 推送）
- `/root/yt-cookie/push.log` —— 推送脚本自身
