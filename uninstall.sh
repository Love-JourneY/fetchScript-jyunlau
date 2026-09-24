#!/usr/bin/env bash
# fetchscript 卸载 —— 干净移除，不留残留；--purge 连状态位一起删
set -euo pipefail
PREFIX="${FETCHSCRIPT_PREFIX:-/opt/fetchscript}"
BIN="${FETCHSCRIPT_BIN_DIR:-/usr/local/bin}/fetchscript"
STATE_DIR="${FETCHSCRIPT_STATE_DIR:-/var/lib/fetchscript}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }
echo "== fetchscript uninstall =="
run systemctl disable --now fetchscript.service 2>/dev/null || true
run rm -f /etc/systemd/system/fetchscript.service
run systemctl daemon-reload 2>/dev/null || true
run rm -f "$BIN"
run rm -rf "$PREFIX"
run rm -rf /etc/fetchscript
run rm -f /usr/local/bin/fetchscript
if [ "$PURGE" = 1 ]; then run rm -rf "$STATE_DIR"; else echo "  保留状态位 $STATE_DIR（要删加 --purge）"; fi
echo "== done =="
