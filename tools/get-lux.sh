#!/usr/bin/env bash
# 多镜像循环下载 lux(8.3MB) → 校验 sha256 → 解包到 vendor/，成功即退出 0
set -u
D="$(cd "$(dirname "$0")/.." && pwd)/vendor"
mkdir -p "$D"
cs="$D/lux_0.24.1_checksums.txt"
if [ ! -s "$cs" ]; then
  curl -fsSL --retry 3 -o "$cs" "https://github.com/iawia002/lux/releases/download/v0.24.1/lux_0.24.1_checksums.txt" || exit 3
fi
want=$(grep -i "Linux_x86_64.tar.gz" "$cs" | awk '{print $1}')
[ -n "$want" ] || { echo "no checksum entry"; exit 3; }
URLS=(
  "https://github.com/iawia002/lux/releases/download/v0.24.1/lux_0.24.1_Linux_x86_64.tar.gz"
  "https://ghfast.top/https://github.com/iawia002/lux/releases/download/v0.24.1/lux_0.24.1_Linux_x86_64.tar.gz"
  "https://gh-proxy.com/https://github.com/iawia002/lux/releases/download/v0.24.1/lux_0.24.1_Linux_x86_64.tar.gz"
  "https://ghproxy.net/https://github.com/iawia002/lux/releases/download/v0.24.1/lux_0.24.1_Linux_x86_64.tar.gz"
)
for round in 1 2 3; do
  for u in "${URLS[@]}"; do
    rm -f "$D/lux_0.24.1_Linux_x86_64.tar.gz"
    echo "[$(date +%H:%M:%S)] round$round try $u"
    if timeout 180 curl -fsSL --retry 2 --retry-delay 2 -o "$D/lux_0.24.1_Linux_x86_64.tar.gz" "$u"; then
      got=$(sha256sum "$D/lux_0.24.1_Linux_x86_64.tar.gz" | awk '{print $1}')
      if [ "$got" = "$want" ]; then
        tar -xzf "$D/lux_0.24.1_Linux_x86_64.tar.gz" -C "$D" && chmod +x "$D/lux" && rm -f "$D/lux_0.24.1_Linux_x86_64.tar.gz"
        echo "OK $("$D/lux" -v 2>&1 | head -1)"; exit 0
      fi
      echo "  checksum mismatch"
    fi
  done
done
echo "FAILED all mirrors"; exit 1
