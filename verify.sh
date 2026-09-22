#!/usr/bin/env bash
# yuanliu 验收 —— 给出通过/失败清单，不靠"看着像好了"
set -uo pipefail

PREFIX="${YUANLIU_PREFIX:-/opt/yuanliu}"
BIN="${YUANLIU_BIN_DIR:-/usr/local/bin}/yuanliu"
STATE_DIR="${YUANLIU_STATE_DIR:-/var/lib/yuanliu}"
B2T_SHARE_LINKS="${B2T_SHARE_LINKS:-/opt/bili2text/app/src/b2t/share_links.py}"

pass=0; fail=0
ok()   { printf '  \033[1;32m[PASS]\033[0m %s\n' "$*"; pass=$((pass+1)); }
no()   { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*"; fail=$((fail+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

head_ "1. 安装位"
[ -d "$PREFIX/lib/yuanliu" ] && ok "程序位存在 $PREFIX/lib/yuanliu" || no "程序位缺失"
[ -x "$BIN" ] && ok "启动器可执行 $BIN" || no "启动器缺失"
[ -d "$STATE_DIR" ] && ok "状态位存在 $STATE_DIR" || no "状态位缺失"
if [ -f "$PREFIX/lib/yuanliu/share_links.py" ]; then
  if grep -q "def parse_share_text" "$PREFIX/lib/yuanliu/share_links.py"; then
    ok "share_links 已随包"
  else
    no "share_links 内容异常"
  fi
fi

head_ "2. 单元自检（纯 stdlib，离线）"
if out=$(PYTHONPATH="$PREFIX/lib" /usr/bin/python3 - <<'PY' 2>&1
from yuanliu import plan, parse_share_text
from yuanliu.resolve import UnsupportedPlatform
link = parse_share_text("7.94 复制打开抖音 https://v.douyin.com/iRNBho6G/ 复制此链接")
assert link.platform == "douyin", link.platform
p = plan("看这个 https://www.bilibili.com/video/BV18puh6wEmt?spm_id_from=333.999")
assert p.platforms == "bilibili", p.platforms
assert p.link.url == "https://www.bilibili.com/video/BV18puh6wEmt", p.link.url
try:
    plan("https://mp.weixin.qq.com/s/abc")
except UnsupportedPlatform:
    pass
else:
    raise AssertionError("视频号应被拒绝")
print("SELFTEST-OK")
PY
); then
  echo "$out" | grep -q SELFTEST-OK && ok "离线自检通过" || no "离线自检未通过：$out"
else
  no "离线自检执行失败：$out"
fi

head_ "3. 引擎"
if [ -r /etc/yuanliu/env ]; then ok "引擎配置存在 /etc/yuanliu/env"; else no "缺少 /etc/yuanliu/env"; fi
engines_json="$(PYTHONPATH="$PREFIX/lib" /usr/bin/python3 -c 'import json;from yuanliu import available_engines;print(json.dumps(available_engines()))' 2>/dev/null)"
echo "         $engines_json"
ytdlp="$(printf '%s' "$engines_json" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin).get("yt-dlp") or "")')"
lux="$(printf '%s' "$engines_json" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin).get("lux") or "")')"
[ -n "$ytdlp" ] && ok "yt-dlp 可用 → $ytdlp" || no "yt-dlp 不可用（B站/抖音/小红书路线会缺一半）"
if [ -n "$lux" ]; then
  ok "lux 可用 → $lux"
  # ⚠️ 别用 `cmd | head -1 | grep -q`：pipefail + SIGPIPE 会把它误判成失败（实测踩过）
  lux_ver="$("$lux" -v 2>&1 || true)"   # 不要接 head：SIGPIPE 会把输出吞掉
  case "$lux_ver" in
    *version*) ok "lux 版本可读：$lux_ver" ;;
    *) no "lux 无法执行（输出：$lux_ver）" ;;
  esac
else
  no "lux 不可用（快手没有别的路线；跑 tools/get-lux.sh 装上）"
fi

head_ "4. 行为（离线命令）"
if "$BIN" engines >/dev/null 2>&1; then ok "yuanliu engines 正常"; else no "yuanliu engines 失败"; fi
if "$BIN" plan "https://www.bilibili.com/video/BV18puh6wEmt" 2>/dev/null | grep -q "platform: bilibili"; then
  ok "plan 识别 B 站"
else
  no "plan 识别 B 站失败"
fi
if "$BIN" plan "https://mp.weixin.qq.com/s/abc" >/dev/null 2>&1; then
  no "视频号本应被拒绝（退出码非 0）"
else
  ok "视频号被明确拒绝（不会静默失败）"
fi

head_ "5. 与 b2t 的副本一致性（防漂移）"
if [ -f "$B2T_SHARE_LINKS" ] && [ -f "$PREFIX/lib/yuanliu/share_links.py" ]; then
  a=$(sha256sum "$B2T_SHARE_LINKS" | awk '{print $1}')
  b=$(sha256sum "$PREFIX/lib/yuanliu/share_links.py" | awk '{print $1}')
  [ "$a" = "$b" ] && ok "share_links.py 两副本一致（$a）" || no "share_links.py 已漂移：b2t=$a yuanliu=$b（改一处必须同步另一处）"
else
  ok "b2t 未安装 ⇒ 跳过一致性检查"
fi

head_ "6. 常驻服务（平板接入）"
PORT="$(grep -h YUANLIU_PORT /etc/yuanliu/env 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8901}"
TOKEN="$(grep -h YUANLIU_TOKEN /etc/yuanliu/token 2>/dev/null | cut -d= -f2)"
if systemctl is-enabled --quiet yuanliu.service 2>/dev/null; then ok "服务已 enable（开机自起）"; else no "服务未 enable"; fi
if systemctl is-active --quiet yuanliu.service 2>/dev/null; then ok "服务运行中"; else no "服务没在跑（journalctl -u yuanliu）"; fi
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then ok "端口 $PORT 正在监听"; else no "端口 $PORT 没有监听"; fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/health" || true)"
[ "$code" = "200" ] && ok "/api/health 免 token 可访问" || no "/api/health 异常（http=$code）"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/jobs" || true)"
[ "$code" = "401" ] && ok "无 token 被拒（fail-closed）" || no "无 token 居然能访问（http=$code）"
if [ -n "$TOKEN" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/jobs?k=$TOKEN" || true)"
  [ "$code" = "200" ] && ok "带 token 可访问" || no "带 token 仍失败（http=$code）"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/files/%2e%2e%2f%2e%2e%2fetc%2fpasswd?k=$TOKEN" || true)"
  [ "$code" = "404" ] && ok "文件接口挡穿越" || no "文件接口疑似可穿越（http=$code）"
else
  no "读不到 token（/etc/yuanliu/token）"
fi

head_ "7. 反向扫描（残留检查）"
stray=$(find "$HOME" -maxdepth 3 -name "yuanliu" -not -path "*/dev/*" -not -path "*/.dsh/*" 2>/dev/null | head -3)
[ -z "$stray" ] && ok "家目录无 yuanliu 残留" || no "家目录有残留：$stray"

printf '\n\033[1m══ 汇总 ══\033[0m\n  通过 \033[1;32m%d\033[0m 项,失败 \033[1;31m%d\033[0m 项\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
