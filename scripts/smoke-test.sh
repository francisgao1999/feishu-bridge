#!/usr/bin/env bash
# OpenCode 服务冒烟测试。
#
# 用法：
#   scripts/smoke-test.sh --users config/users.json
#   scripts/smoke-test.sh --url http://10.0.0.11:4096 --password <pw>
#   scripts/smoke-test.sh --url ... --password ... --username opencode
#
# 每个用户依次验证：
#   1. GET /global/health 返回 200
#   2. GET /api/model 无认证 401，带认证 200
#   3. 写 2 MB 随机数据到 /tmp/opencode-smoke.bin
#   4. 读回并比对 sha256
#   5. GET /api/event 能建立 SSE（拿到 200 即断开）
#
# 任一失败退出码为 1，并打印失败用户与失败步骤。
set -uo pipefail

SMOKE_PATH="/tmp/opencode-smoke.bin"
SMOKE_SIZE=2097152
TMPDIR_BASE="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "${TMPDIR_BASE%/}/opencode-smoke.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

FAILURES=0
CHECKED=0

usage() {
  cat <<'EOF'
用法:
  scripts/smoke-test.sh --users <users.json> [--username <user>]
  scripts/smoke-test.sh --url <url> --password <pw> [--username <user>]

参数:
  --users <path>      users.json 路径；对其中的每个用户执行测试
  --url <url>         OpenCode 服务地址，例如 http://10.0.0.11:4096
  --password <pw>     OpenCode 密码（OPENCODE_PASSWORD）
  --username <user>   basic auth 用户名，默认 opencode
  --help              显示本帮助

退出码:
  0  全部通过
  1  至少一个用户失败
  2  参数错误
EOF
}

die() {
  echo "错误: $*" >&2
  exit 2
}

log() {
  printf '%s\n' "$*"
}

fail() {
  local who="$1" step="$2" detail="$3"
  FAILURES=$((FAILURES + 1))
  printf '❌ %s  %s: %s\n' "$who" "$step" "$detail"
}

ok() {
  local who="$1" step="$2"
  printf '✅ %s  %s\n' "$who" "$step"
}

# --- 参数解析 --------------------------------------------------------------
USERS_FILE=""
URL=""
PASSWORD=""
USERNAME="opencode"

while [ $# -gt 0 ]; do
  case "$1" in
    --users)
      [ $# -ge 2 ] || die "--users 缺少参数"
      USERS_FILE="$2"; shift 2 ;;
    --url)
      [ $# -ge 2 ] || die "--url 缺少参数"
      URL="$2"; shift 2 ;;
    --password)
      [ $# -ge 2 ] || die "--password 缺少参数"
      PASSWORD="$2"; shift 2 ;;
    --username)
      [ $# -ge 2 ] || die "--username 缺少参数"
      USERNAME="$2"; shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      die "未知参数: $1" ;;
  esac
done

if [ -n "$USERS_FILE" ] && [ -n "$URL" ]; then
  die "--users 与 --url 不能同时使用"
fi
if [ -z "$USERS_FILE" ] && { [ -z "$URL" ] || [ -z "$PASSWORD" ]; }; then
  usage >&2
  exit 2
fi
if [ -n "$USERS_FILE" ] && [ ! -f "$USERS_FILE" ]; then
  die "users 文件不存在: $USERS_FILE"
fi

command -v curl >/dev/null 2>&1 || die "需要 curl"
command -v python3 >/dev/null 2>&1 || die "需要 python3"
command -v sha256sum >/dev/null 2>&1 || die "需要 sha256sum"

# --- 单用户检查 ------------------------------------------------------------
# check_user <label> <url> <password>
check_user() {
  local label="$1" base="$2" pw="$3"
  local auth="$USERNAME:$pw"
  local base="${base%/}"

  CHECKED=$((CHECKED + 1))
  log ""
  log "=== 冒烟测试: $label ($base) ==="

  # 1. health
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --max-time 10 "$base/global/health" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    ok "$label" "GET /global/health 200"
  else
    fail "$label" "GET /global/health" "返回 $code"
    return
  fi

  # 2. 鉴权
  code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --max-time 10 "$base/api/model" 2>/dev/null || true)"
  if [ "$code" = "401" ]; then
    ok "$label" "无认证访问 /api/model 返回 401"
  else
    fail "$label" "无认证访问 /api/model" "期望 401，实际 $code"
  fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --max-time 10 -u "$auth" "$base/api/model" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    ok "$label" "带认证访问 /api/model 返回 200"
  else
    fail "$label" "带认证访问 /api/model" "期望 200，实际 $code"
  fi

  # 3. 写入 2 MB
  local src="$WORK_DIR/upload-$CHECKED.bin"
  local dst="$WORK_DIR/readback-$CHECKED.bin"
  head -c "$SMOKE_SIZE" /dev/urandom > "$src" || {
    fail "$label" "生成本地测试数据" "head 失败"
    return
  }
  local want have
  want="$(sha256sum "$src" | awk '{print $1}')"

  code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --max-time 120 -u "$auth" \
      -X POST \
      -H 'Content-Type: application/octet-stream' \
      --data-binary "@$src" \
      "$base/api/experimental/fs/write?path=${SMOKE_PATH}&location%5Bdirectory%5D=%2Ftmp" \
      2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    ok "$label" "写入 $SMOKE_PATH (2 MB)"
  else
    fail "$label" "写入 $SMOKE_PATH" "返回 $code"
    return
  fi

  # 4. 读回。v2 的 fs/read 必须带 location[directory]，否则 404。
  code="$(curl -sS -o "$dst" -w '%{http_code}' \
      --max-time 120 -u "$auth" \
      "$base/api/fs/read/%2Ftmp%2Fopencode-smoke.bin?location%5Bdirectory%5D=%2Ftmp" \
      2>/dev/null || true)"
  if [ "$code" != "200" ]; then
    fail "$label" "读取 $SMOKE_PATH" "返回 $code"
    return
  fi
  have="$(sha256sum "$dst" | awk '{print $1}')"
  if [ "$want" = "$have" ]; then
    ok "$label" "读回内容 sha256 一致"
  else
    fail "$label" "读回内容 sha256" "本地 $want / 远端 $have"
  fi

  # 5. SSE
  code="$(curl -sS -N -o /dev/null -w '%{http_code}' \
      --max-time 3 -u "$auth" "$base/api/event" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    ok "$label" "SSE /api/event 建立成功"
  else
    fail "$label" "SSE /api/event" "返回 $code"
  fi
}

# --- 执行 ------------------------------------------------------------------
if [ -n "$USERS_FILE" ]; then
  # 用 python3 解析 users.json，逐行输出 url<TAB>password<TAB>slug
  mapfile -t ENTRIES < <(python3 - "$USERS_FILE" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        users = json.load(fh)
except Exception as exc:
    print(f"__ERROR__\t{exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(users, list):
    print("__ERROR__\tusers.json 顶层必须是数组", file=sys.stderr)
    sys.exit(1)
for item in users:
    if not isinstance(item, dict):
        continue
    url = str(item.get("url") or "").rstrip("/")
    pw = str(item.get("password") or "")
    slug = str(item.get("slug") or item.get("open_id") or "?")
    if not url or not pw:
        print(f"__ERROR__\t{slug} 缺少 url 或 password", file=sys.stderr)
        sys.exit(1)
    print(f"{url}\t{pw}\t{slug}")
PY
  ) || {
    echo "❌ 解析 users.json 失败" >&2
    exit 1
  }

  if [ "${#ENTRIES[@]}" -eq 0 ]; then
    echo "❌ users.json 中没有可测试的用户" >&2
    exit 1
  fi

  for entry in "${ENTRIES[@]}"; do
    IFS=$'\t' read -r u p s <<< "$entry"
    check_user "$s" "$u" "$p"
  done
else
  check_user "manual" "$URL" "$PASSWORD"
fi

log ""
if [ "$FAILURES" -eq 0 ]; then
  log "全部通过（$CHECKED 个用户）"
  exit 0
fi
log "失败：$FAILURES 项（共 $CHECKED 个用户）"
exit 1
