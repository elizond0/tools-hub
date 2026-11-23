#!/bin/sh
# =============================================================================
# 相册展示 · cloudflared 守护 watchdog（对齐 PDF boot.sh v3 的进程守护 + 凭证失效自愈）
#
# 设计原则（读容器实际 boot.sh / base-sync.sh 后定稿）：
#   - 只补两件真正缺的事：① cloudflared 退出即重启 ② 凭证失效(Tunnel not found)主动换域名
#   - 地址同步【不接管】：base-sync.sh 已有「推送前 curl index.json 拿 200 才推 + 推完回读校验」，
#     比本脚本的精简版健壮。它继续每 300s 跑，两者职责分离、不双写 base.json。
#   - 不碰 Caddyfile / album-push.py / ffmpeg / totp，不改 boot.sh 的功能，只在末尾多起一个守护。
#
# 落地位置：/var/lib/tailscale/cf-watchdog.sh
# 调用：boot.sh 里把原来那行 "$P/cloudflared tunnel ... &" 替换为 "sh $P/cf-watchdog.sh &"
#       （其余 base-sync.sh 那行保持不动）
#
# 凭据：不读 PAT、不写 GitHub——地址同步交给 base-sync.sh，本脚本只管进程活着与日志。
# 依赖：cloudflared 二进制在 $P/cloudflared（相册现状，非 PATH），grep/sleep 为 busybox 自带。
# 关键参数 --protocol http2：2026-11-23 实测该 NAS 出口 QUIC/UDP 被阻，默认 QUIC 反复超时导致隧道 530；
#   强制 HTTP/2(TCP) 后 Registered 成功、health 200。此参数必须保留，勿删。
# =============================================================================

P=${ALBUM_HOME:-/var/lib/tailscale}
CFLOG=$P/cf.log
LOG=$P/watchdog.log
CFBIN=$P/cloudflared
RESTART_DELAY=3
CHECK_INTERVAL=60

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"; }

chmod +x "$CFBIN" 2>/dev/null

# ---------- 守护循环：cloudflared 一旦退出就重启 ----------
(
  while true; do
    "$CFBIN" tunnel --url http://127.0.0.1:8443 --no-autoupdate --protocol http2 >"$CFLOG" 2>&1
    log "cloudflared exited, restart in ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
  done
) &

# ---------- 凭证失效自愈：新增 Tunnel not found 就 pkill 让守护循环申请新域名 ----------
last_err=0
while true; do
  sleep "$CHECK_INTERVAL"

  cur_err=$(grep -c 'Tunnel not found' "$CFLOG" 2>/dev/null || echo 0)
  if [ "$cur_err" -gt "$last_err" ]; then
    log "Tunnel not found $last_err->$cur_err, killing cloudflared for new tunnel"
    pkill -f 'cloudflared tunnel' 2>/dev/null
    last_err=$cur_err
    sleep 10                       # 等守护循环拉起进程申请新域名
  else
    last_err=$cur_err
  fi
done
