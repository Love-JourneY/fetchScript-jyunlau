#!/usr/bin/env bash
# yuanliu 卸载 —— 干净移除，不留残留；--purge 连状态位一起删
set -euo pipefail
PREFIX="${YUANLIU_PREFIX:-/opt/yuanliu}"
BIN="${YUANLIU_BIN_DIR:-/usr/local/bin}/yuanliu"
STATE_DIR="${YUANLIU_STATE_DIR:-/var/lib/yuanliu}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }
echo "== yuanliu uninstall =="
run systemctl disable --now yuanliu.service 2>/dev/null || true
run rm -f /etc/systemd/system/yuanliu.service
run systemctl daemon-reload 2>/dev/null || true
run rm -f "$BIN"
run rm -rf "$PREFIX"
run rm -rf /etc/yuanliu
run rm -f /usr/local/bin/yuanliu
if [ "$PURGE" = 1 ]; then run rm -rf "$STATE_DIR"; else echo "  保留状态位 $STATE_DIR（要删加 --purge）"; fi
echo "== done =="
