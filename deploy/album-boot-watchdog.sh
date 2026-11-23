#!/bin/sh
# =============================================================================
# 相册展示 · 隧道自愈 watchdog（对齐 PDF boot.sh v3 的三件事）
# 目标：把 PDF 已经在用的三种自愈能力移植到相册，不改动现有 Caddyfile / 门禁 / ffmpeg / totp 逻辑。
#
# 三件事：
#   1) cloudflared 进程守护：退出即 3 秒重启（治"进程死了没人拉"）
#   2) 凭证失效自愈：cf.log 新增 "Tunnel not found" → pkill 让守护循环重新申请新域名（治 quick tunnel 凭证被回收）
#   3) 地址强制回滚：每 60s 比对远端 base.json 与本地最新域名，不一致就用 Contents API 纠正
#      （治外部篡改 / 同步循环卡死 / 写回失效域名，对应 PDF 坑 16 的 851 次写回事故）
#
# 落地位置：容器内 /var/lib/tailscale/boot-watchdog.sh（与现有 boot.sh 同目录）
# 调用方式：见同目录《相册自愈-落地指引.md》——由 boot.sh 替换其"直接起 cloudflared"那一段，
#           本脚本自身不重复起 Caddy、不碰 /srv/pub。
# 依赖：curl、jq、base64、pkill（procps）、cloudflared —— 与 PDF 容器同款，相册 Alpine 已装 cloudflared。
# 凭据：GitHub PAT 从 /var/lib/tailscale/.gh_token 读取（沿用相册现有文件，不新增、不落明文到脚本）。
# =============================================================================

# ---------- 可配置项（与相册现有部署保持一致，改动只需改这里）----------
LOG=/var/lib/tailscale/watchdog.log          # 自愈事件日志（新文件；健康运行时不增长）
CFLOG=/var/lib/tailscale/cf.log              # cloudflared 日志（相册现有位置，非 PDF 的 /shared/cf.log）
TOKEN_FILE=/var/lib/tailscale/.gh_token      # GitHub PAT（相册现有位置）
REPO=elizond0/album                          # 相册仓库（PDF 是 pdf-tools）
ADDR_FILE=base.json                          # 地址文件（PDF 是 nas.json；相册字段是 base 不是 url）
LOCAL_URL=http://localhost:8443              # cloudflared 回源地址（相册 Caddy 监听 8443，与 PDF 一致）
CHECK_INTERVAL=60                            # 自愈检查间隔（秒）
RESTART_DELAY=3                              # 进程退出后重启延迟（秒）

# ---------- 补装依赖（幂等；相册 boot.sh 已装 caddy/ffmpeg/python，这里只补 watchdog 需要的）----------
apk add --no-cache curl jq procps >/dev/null 2>&1

GH_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null)
API="https://api.github.com/repos/$REPO/contents/$ADDR_FILE"
RAW="https://raw.githubusercontent.com/$REPO/main/$ADDR_FILE"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"; }

# ============ 第 1 件事：cloudflared 守护循环（退出即重启）============
# 注意：调用方 boot.sh 必须删掉原来的"直接起 cloudflared"那行，否则双开抢日志与端口，自愈失效（PDF 坑：compose 同时直接启动会双开）。
(
  while true; do
    cloudflared tunnel --no-autoupdate --url "$LOCAL_URL" --logfile "$CFLOG" >>/dev/null 2>&1
    log "cloudflared exited, restart in ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
  done
) &

# ============ 第 2、3 件事：凭证失效换域名 + 地址强制回滚 ============
last_err=0
while true; do
  sleep "$CHECK_INTERVAL"

  # --- 第 2 件事：凭证失效自愈 ---
  # 只认"相对上次新增"的错误，避免历史错误导致反复重启（PDF 附录 C 同款逻辑）。
  cur_err=$(grep -c 'Tunnel not found' "$CFLOG" 2>/dev/null || echo 0)
  if [ "$cur_err" -gt "$last_err" ]; then
    log "Tunnel not found $last_err->$cur_err, killing cloudflared for new tunnel"
    pkill -f 'cloudflared tunnel' 2>/dev/null
    last_err=$cur_err
    sleep 10                       # 等守护循环拉起新进程并申请新域名
    continue                       # 本轮直接进下一次，抓新域名同步
  fi

  # --- 第 3 件事：地址强制回滚 ---
  # 抓 cf.log 里最后一个域名（新隧道申请后会追加到末尾）
  url=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$CFLOG" 2>/dev/null | tail -1)
  [ -n "$url" ] || continue

  # 读远端 base.json，只要包含当前域名就认为一致（沿用 PDF v2 的容错比对）
  remote=$(curl -s --max-time 20 -H "Authorization: Bearer $GH_TOKEN" "$RAW" 2>/dev/null)
  case "$remote" in
    *"$url"*) continue ;;
  esac

  # 不一致：取 sha 后用 Contents API 强制纠正（字段用 base，不是 url！）
  [ -n "$GH_TOKEN" ] || { log "WARN: GH_TOKEN empty, skip PUT"; continue; }
  sha=$(curl -s --max-time 20 -H "Authorization: Bearer $GH_TOKEN" "$API" | jq -r '.sha // empty')
  [ -n "$sha" ] || { log "WARN: cannot get sha, skip PUT"; continue; }

  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  content=$(printf '{"base":"%s","updated":"%s"}' "$url" "$ts" | base64 | tr -d '\n')
  if curl -s --max-time 30 -o /dev/null -X PUT \
      -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
      "$API" -d "{\"message\":\"album: tunnel url sync ($ts)\",\"content\":\"$content\",\"sha\":\"$sha\"}"; then
    log "PUT ok $url"
  else
    log "WARN: PUT failed for $url"
  fi
done

wait
