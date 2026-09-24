#!/usr/bin/env bash
# fetchscript 安装器 —— 幂等、可重复跑、带 --dry-run
#
# 落位（FHS）：
#   /opt/fetchscript/lib/fetchscript   ← 程序位（纯 stdlib，零第三方依赖）
#   /opt/fetchscript/vendor/lux          ← 随包二进制（离线自洽；yt-dlp 复用 b2t 那份）
#   /usr/local/bin/fetchscript           ← 启动器
#   /var/lib/fetchscript/                ← 状态位（默认下载目录）
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${FETCHSCRIPT_PREFIX:-/opt/fetchscript}"
BIN_DIR="${FETCHSCRIPT_BIN_DIR:-/usr/local/bin}"
STATE_DIR="${FETCHSCRIPT_STATE_DIR:-/var/lib/fetchscript}"
PYTHON="${FETCHSCRIPT_PYTHON:-/usr/bin/python3}"
YTDLP_HINT="${FETCHSCRIPT_YTDLP:-/opt/bili2text/app/.venv/bin/yt-dlp}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

say() { printf '  %s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then say "would: $*"; else "$@"; fi
}

echo "== fetchscript install =="
say "模块目录 : $MODULE_DIR"
say "程序位   : $PREFIX"
say "启动器   : $BIN_DIR/fetchscript"
say "状态位   : $STATE_DIR"
say "python   : $PYTHON"
[ "$DRY_RUN" = 1 ] && say "(dry-run：只打印，不落盘)"

run install -d -m 755 "$PREFIX/lib" "$PREFIX/vendor" "$PREFIX/tools"
run rm -rf "$PREFIX/lib/fetchscript"
run cp -a "$MODULE_DIR/lib/fetchscript" "$PREFIX/lib/"
run find "$PREFIX/lib" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
run chmod -R a+rX "$PREFIX/lib"

# 浏览器嗅探脚本（node，走 Playwright）
if [ -f "$MODULE_DIR/tools/sniff.cjs" ]; then
  run install -m 644 "$MODULE_DIR/tools/sniff.cjs" "$PREFIX/tools/sniff.cjs"
  say "sniff.cjs: 已安装（抖音/小红书走真浏览器嗅探）"
fi
if command -v node >/dev/null 2>&1; then say "node     : $(command -v node)"; else say "node     : 缺失 ⇒ 浏览器嗅探不可用"; fi

# 随包二进制：有就装上，没有不算失败（引擎是可选后端）
if [ -x "$MODULE_DIR/vendor/lux" ]; then
  run install -m 755 "$MODULE_DIR/vendor/lux" "$PREFIX/vendor/lux"
  say "lux      : 已随包安装"
else
  say "lux      : 随包没有（可跑 tools/get-lux.sh 下载，装上后快手/小红书/微博才可用）"
fi

run install -d -m 755 "$STATE_DIR"
if id bubu12 >/dev/null 2>&1; then
  run chown bubu12:wheel "$STATE_DIR"
fi

# 引擎路径配置（存在才写，避免把不存在的路径钉死）
if [ "$DRY_RUN" = 0 ]; then
  install -d -m 755 /etc/fetchscript
  {
    echo "# fetchscript 引擎路径（由 install.sh 生成）"
    if [ -x "$YTDLP_HINT" ]; then echo "FETCHSCRIPT_YTDLP=$YTDLP_HINT"; fi
    if [ -x "$PREFIX/vendor/lux" ]; then echo "FETCHSCRIPT_LUX=$PREFIX/vendor/lux"; fi
    PW=""
    for cand in /opt/fetchscript/vendor/node_modules/playwright "$HOME/dev/qwen-audio-agent/node_modules/playwright"; do
      [ -d "$cand" ] && PW="$cand" && break
    done
    if [ -n "$PW" ]; then echo "FETCHSCRIPT_PLAYWRIGHT=$PW"; fi
  } > /etc/fetchscript/env
  chmod 644 /etc/fetchscript/env
  say "配置     : /etc/fetchscript/env"
fi

# 常驻服务：token（600，只 root 可读）+ systemd 单元
if [ "$DRY_RUN" = 0 ]; then
  install -d -m 755 /etc/fetchscript
  if [ ! -s /etc/fetchscript/token ]; then
    printf 'FETCHSCRIPT_TOKEN=%s\n' "$(/usr/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" > /etc/fetchscript/token
    chmod 640 /etc/fetchscript/token
    chgrp wheel /etc/fetchscript/token 2>/dev/null || true
    say "token    : 已生成 /etc/fetchscript/token（640 root:wheel —— 自己（wheel 组）可读，用于拼平板 URL）"
  else
    # 自愈：老版本（或改名迁移过来的）token 文件里可能是旧变量名，会导致服务"没有 token"起不来
    if grep -q '^MEDIA_RESOLVE_TOKEN=' /etc/fetchscript/token 2>/dev/null; then
      sed -i 's/^MEDIA_RESOLVE_TOKEN=/FETCHSCRIPT_TOKEN=/' /etc/fetchscript/token
      say "token    : 旧变量名已修正为 FETCHSCRIPT_TOKEN"
    fi
    say "token    : 已存在，沿用"
  fi
  # 端口/目录等非敏感配置，追加进 env（可被用户读，供 CLI 用）
  grep -q FETCHSCRIPT_PORT /etc/fetchscript/env 2>/dev/null || {
    {
      echo "FETCHSCRIPT_PORT=${FETCHSCRIPT_PORT:-8901}"
      echo "FETCHSCRIPT_BIND=${FETCHSCRIPT_BIND:-0.0.0.0}"
      echo "FETCHSCRIPT_STATE_DIR=$STATE_DIR"
      # ⚠️ install.sh 以 root 跑时 $HOME=/root ⇒ 必须显式取目标用户的家目录（实测事故）
      RUN_HOME="$(getent passwd "${FETCHSCRIPT_USER:-bubu12}" 2>/dev/null | cut -d: -f6)"
      RUN_HOME="${RUN_HOME:-/home/bubu12}"
      echo "FETCHSCRIPT_COOKIES=${FETCHSCRIPT_COOKIES:-$RUN_HOME/.cache/fetchscript/cookies.txt}"
    } >> /etc/fetchscript/env
  }
  if [ -f "$MODULE_DIR/systemd/fetchscript.service" ]; then
    install -m 644 "$MODULE_DIR/systemd/fetchscript.service" /etc/systemd/system/fetchscript.service
    systemctl daemon-reload
    systemctl enable --now fetchscript.service >/dev/null 2>&1 || say "服务启动失败，看 journalctl -u fetchscript"
    say "服务     : fetchscript.service 已启用（端口 ${FETCHSCRIPT_PORT:-8901}）"
  fi
  # 防火墙（有才开，没有就跳过）
  if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
    firewall-cmd --add-port="${FETCHSCRIPT_PORT:-8901}/tcp" --permanent >/dev/null && firewall-cmd --reload >/dev/null && say "防火墙   : 已放行 ${FETCHSCRIPT_PORT:-8901}/tcp"
  elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q active; then
    ufw allow "${FETCHSCRIPT_PORT:-8901}/tcp" >/dev/null && say "防火墙   : ufw 已放行"
  else
    say "防火墙   : 未检测到启用中的防火墙（无需放行）"
  fi
fi

# 启动器
if [ "$DRY_RUN" = 0 ]; then
  cat > "$BIN_DIR/fetchscript" <<EOF
#!/usr/bin/env bash
# fetchscript 启动器（由 install.sh 生成，别手改）
set -euo pipefail
[ -r /etc/fetchscript/env ] && set -a && . /etc/fetchscript/env && set +a
export PYTHONPATH="$PREFIX/lib\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON" -m fetchscript "\$@"
EOF
  chmod 755 "$BIN_DIR/fetchscript"
  say "启动器已写"
fi

echo "== done =="
[ "$DRY_RUN" = 0 ] && { "$BIN_DIR/fetchscript" engines | head -20 || true; }
