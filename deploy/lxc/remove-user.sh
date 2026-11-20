#!/usr/bin/env bash
# 归档并删除一个用户 LXC，并从 users.json 中移除映射。
#
# 必须在 PVE 宿主机上以 root 执行。
# 用法见 docs/lxc-scripts.md。
set -euo pipefail

DOCKER_LXC=100
CONFIG_PATH=/opt/feishu-bridge/config/users.json
BACKUP_DIR=/var/lib/vz/dump
KEEP_LXC=0
DRY_RUN=0

SLUG=""
VMID=""

usage() {
  cat <<'EOF'
用法：
  ./deploy/lxc/remove-user.sh <slug> [选项]
  ./deploy/lxc/remove-user.sh --vmid <id> [选项]

选项：
  --vmid <id>           直接指定 LXC ID（与 slug 二选一）
  --docker-lxc <id>     运行 bridge 的 docker LXC ID（默认 100）
  --config-path <path>  docker LXC 内 users.json 路径
                        （默认 /opt/feishu-bridge/config/users.json）
  --backup-dir <path>   vzdump 输出目录（默认 /var/lib/vz/dump）
  --keep-lxc            只从 users.json 移除，不停止/删除 LXC
  --dry-run             只打印将执行的操作
  -h, --help            显示本帮助

说明：
  默认流程：停止 LXC → vzdump 归档 → pct destroy → 从 users.json 移除。
  归档文件保留在 --backup-dir，可用 pct restore 恢复。
EOF
}

die() {
  echo "错误：$*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $*"
    return 0
  fi
  "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --vmid) VMID="$2"; shift 2 ;;
    --docker-lxc) DOCKER_LXC="$2"; shift 2 ;;
    --config-path) CONFIG_PATH="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --keep-lxc) KEEP_LXC=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "未知选项：$1" ;;
    *)
      if [ -z "$SLUG" ]; then SLUG="$1"; else die "多余参数：$1"; fi
      shift ;;
  esac
done

if [ -z "$SLUG" ] && [ -z "$VMID" ]; then
  usage
  die "必须提供 slug 或 --vmid"
fi
if [ -n "$SLUG" ] && [ -n "$VMID" ]; then
  die "slug 与 --vmid 只能二选一"
fi

command -v pct >/dev/null 2>&1 || die "未找到 pct，请在 PVE 宿主机上执行"
command -v python3 >/dev/null 2>&1 || die "未找到 python3"
command -v vzdump >/dev/null 2>&1 || die "未找到 vzdump"

# --- 定位 VMID -------------------------------------------------------------
if [ -z "$VMID" ]; then
  info "按主机名 oc-$SLUG 查找 VMID"
  VMID=$(pct list | awk -v host="oc-$SLUG" '$NF == host {print $1; exit}')
  [ -n "$VMID" ] || die "未找到主机名为 oc-$SLUG 的 LXC"
fi

pct config "$VMID" >/dev/null 2>&1 || die "LXC $VMID 不存在"
LXC_HOST=$(pct config "$VMID" | awk -F': ' '/^hostname:/ {print $2}')
info "目标 LXC：$VMID（${LXC_HOST:-未知主机名}）"

# --- 归档 ------------------------------------------------------------------
if [ "$KEEP_LXC" -eq 0 ]; then
  info "停止 LXC $VMID"
  run pct stop "$VMID" || true

  mkdir -p "$BACKUP_DIR"
  info "归档到 $BACKUP_DIR"
  run vzdump "$VMID" --dumpdir "$BACKUP_DIR" --mode stop --compress zstd

  info "删除 LXC $VMID"
  run pct destroy "$VMID" --purge
else
  info "跳过停止/归档/删除（--keep-lxc）"
fi

# --- 更新 users.json -------------------------------------------------------
info "从 users.json 移除映射"
if [ "$DRY_RUN" -eq 0 ]; then
  if pct exec "$DOCKER_LXC" -- test -f "$CONFIG_PATH" 2>/dev/null; then
    TMP_USERS=$(mktemp /tmp/users.json.XXXXXX)
    pct exec "$DOCKER_LXC" -- cat "$CONFIG_PATH" > "$TMP_USERS"

    python3 - "$TMP_USERS" "$SLUG" "$LXC_HOST" <<'PY'
import json, sys

path, slug, hostname = sys.argv[1:4]
target_host = hostname or ""
with open(path, encoding="utf-8") as handle:
    users = json.load(handle)
if not isinstance(users, list):
    sys.exit("users.json 顶层必须是数组")

before = len(users)
users = [
    item for item in users
    if not (
        (slug and item.get("slug") == slug)
        or (target_host and item.get("slug") == target_host.removeprefix("oc-"))
    )
]
if len(users) == before:
    print("警告：users.json 中没有匹配的条目", file=sys.stderr)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(users, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

    pct exec "$DOCKER_LXC" -- sh -c \
      "cp '$CONFIG_PATH' '$CONFIG_PATH.bak'"
    pct push "$DOCKER_LXC" "$TMP_USERS" "$CONFIG_PATH"
    pct exec "$DOCKER_LXC" -- sh -c \
      "chown 1000:1000 '$CONFIG_PATH' && chmod 600 '$CONFIG_PATH'"
    rm -f "$TMP_USERS"
  else
    echo "警告：$CONFIG_PATH 不存在，跳过 users.json 更新" >&2
  fi
fi

echo
echo "用户已移除：VMID $VMID（${LXC_HOST:-未知主机名}）"
if [ "$KEEP_LXC" -eq 0 ]; then
  echo "归档文件在 $BACKUP_DIR，可用 pct restore 恢复。"
fi
