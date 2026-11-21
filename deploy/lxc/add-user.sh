#!/usr/bin/env bash
# 开通一个用户 LXC：从模板克隆、配置网络与资源、生成密码、
# 启动 opencode-serve、写入 users.json、跑冒烟测试。
#
# 必须在 PVE 宿主机上以 root 执行。
# 用法见 docs/lxc-scripts.md。
set -euo pipefail

TEMPLATE=900
DOCKER_LXC=100
GATEWAY=10.0.0.1
BRIDGE=vmbr0
MEMORY=2048
SWAP=1024
CORES=4
DISK=24G
PREFIX=24
CONFIG_PATH=/opt/feishu-bridge/config/users.json
SKIP_SMOKE=0
DRY_RUN=0

SLUG=""
OPEN_ID=""
IP=""

usage() {
  cat <<'EOF'
用法：
  ./deploy/lxc/add-user.sh <slug> <open_id> <ip> [选项]

位置参数：
  slug      用户名，小写字母开头，只能包含小写字母、数字、连字符，1-21 字符
  open_id   飞书 open_id，形如 ou_xxxxxxxx
  ip        用户 LXC 的固定 IP（必须由管理员指定，脚本不做自动分配）

选项：
  --template <id>       模板 LXC ID（默认 900）
  --docker-lxc <id>     运行 bridge 的 docker LXC ID（默认 100）
  --gateway <ip>        网关（默认 10.0.0.1）
  --bridge <name>       PVE 网桥（默认 vmbr0）
  --prefix <bits>       IP 前缀长度（默认 24）
  --memory <MB>         内存（默认 2048）
  --swap <MB>           swap（默认 1024）
  --cores <n>           CPU 核心数（默认 4）
  --disk <size>         磁盘大小（默认 24G，模板盘 8G，克隆后扩容）
  --config-path <path>  docker LXC 内 users.json 路径
                        （默认 /opt/feishu-bridge/config/users.json）
  --skip-smoke          不执行冒烟测试
  --dry-run             只打印将执行的命令，不实际创建
  -h, --help            显示本帮助

示例：
  ./deploy/lxc/add-user.sh alice ou_abc123 10.0.0.11
  ./deploy/lxc/add-user.sh bob ou_def456 10.0.0.12 --memory 3072 --cores 6

失败处理：
  脚本会在创建前打印 VMID；任一步失败时按提示手工清理：
  pct stop <vmid> && pct destroy <vmid>
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

# --- 参数解析 --------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --template) TEMPLATE="$2"; shift 2 ;;
    --docker-lxc) DOCKER_LXC="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --swap) SWAP="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --config-path) CONFIG_PATH="$2"; shift 2 ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "未知选项：$1" ;;
    *)
      if [ -z "$SLUG" ]; then SLUG="$1"
      elif [ -z "$OPEN_ID" ]; then OPEN_ID="$1"
      elif [ -z "$IP" ]; then IP="$1"
      else die "多余参数：$1"
      fi
      shift ;;
  esac
done

[ -n "$SLUG" ] || { usage; die "缺少 slug"; }
[ -n "$OPEN_ID" ] || { usage; die "缺少 open_id"; }
[ -n "$IP" ] || { usage; die "缺少 ip"; }

# --- 前置检查 --------------------------------------------------------------
command -v pct >/dev/null 2>&1 || die "未找到 pct，请在 PVE 宿主机上执行"
command -v pvesh >/dev/null 2>&1 || die "未找到 pvesh"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl"
command -v python3 >/dev/null 2>&1 || die "未找到 python3"
command -v curl >/dev/null 2>&1 || die "未找到 curl"

[[ "$SLUG" =~ ^[a-z][a-z0-9-]{0,20}$ ]] || die "slug 非法：$SLUG（需匹配 ^[a-z][a-z0-9-]{0,20}$）"
[[ "$OPEN_ID" =~ ^ou_[A-Za-z0-9_-]+$ ]] || die "open_id 非法：$OPEN_ID"
[[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "IP 格式非法：$IP"
[[ "$MEMORY" =~ ^[0-9]+$ ]] || die "--memory 必须是整数"
[[ "$SWAP" =~ ^[0-9]+$ ]] || die "--swap 必须是整数"
[[ "$CORES" =~ ^[0-9]+$ ]] || die "--cores 必须是整数"
[[ "$PREFIX" =~ ^[0-9]{1,2}$ ]] || die "--prefix 必须是 1-32 的整数"
[ "$PREFIX" -ge 1 ] && [ "$PREFIX" -le 32 ] || die "--prefix 必须在 1-32 之间"

info "检查模板 LXC $TEMPLATE 存在"
pct config "$TEMPLATE" >/dev/null 2>&1 || die "模板 LXC $TEMPLATE 不存在"

info "检查 docker LXC $DOCKER_LXC 存在"
pct config "$DOCKER_LXC" >/dev/null 2>&1 || die "docker LXC $DOCKER_LXC 不存在"

info "检查 IP $IP 未被占用"
if ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then
  die "IP $IP 已被占用（ping 有响应）"
fi

info "检查 users.json 中是否已有该 slug / open_id"
if [ "$DRY_RUN" -eq 0 ]; then
  if pct exec "$DOCKER_LXC" -- test -f "$CONFIG_PATH" 2>/dev/null; then
    CHECK_FILE=$(mktemp /tmp/users-check.XXXXXX)
    pct exec "$DOCKER_LXC" -- cat "$CONFIG_PATH" > "$CHECK_FILE"
    python3 - "$CHECK_FILE" "$SLUG" "$OPEN_ID" <<'PY'
import json, sys
path, slug, open_id = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as handle:
        users = json.load(handle)
except json.JSONDecodeError:
    sys.exit("users.json 不是合法 JSON，请先修好再执行")
for item in users:
    if item.get("slug") == slug:
        sys.exit(f"slug {slug} 已存在于 users.json")
    if item.get("open_id") == open_id:
        sys.exit(f"open_id {open_id} 已存在于 users.json")
PY
    rm -f "$CHECK_FILE"
  else
    echo "警告：$CONFIG_PATH 不存在，将创建新文件" >&2
  fi
fi

NEXT_ID=$(pvesh get /cluster/nextid)
info "将使用 VMID $NEXT_ID，主机名 oc-$SLUG"
info "创建失败时请手工清理：pct stop $NEXT_ID && pct destroy $NEXT_ID"

# --- 克隆与配置 ------------------------------------------------------------
info "从模板 $TEMPLATE 克隆 LXC $NEXT_ID"
run pct clone "$TEMPLATE" "$NEXT_ID" --hostname "oc-$SLUG" --full

info "配置网络与资源"
run pct set "$NEXT_ID" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$IP/$PREFIX,gw=$GATEWAY" \
  --memory "$MEMORY" --swap "$SWAP" --cores "$CORES" --onboot 1

info "启动 LXC"
run pct start "$NEXT_ID"

info "等待模板就绪标记 /etc/opencode/ready"
READY=0
for _ in $(seq 1 60); do
  if [ "$DRY_RUN" -eq 1 ]; then READY=1; break; fi
  if pct exec "$NEXT_ID" -- test -f /etc/opencode/ready 2>/dev/null; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" -ne 1 ]; then
  echo "错误：等待就绪标记超时。已创建 VMID $NEXT_ID。" >&2
  echo "清理：pct stop $NEXT_ID && pct destroy $NEXT_ID" >&2
  exit 1
fi

# 在运行中扩容，PVE 会对 ext4 自动执行 resize2fs；停止状态下只扩卷不扩文件系统。
info "扩容磁盘到 $DISK"
run pct resize "$NEXT_ID" rootfs "$DISK"

info "生成独立密码并写入 /etc/opencode/serve.env"
PW=$(openssl rand -hex 24)
if [ "$DRY_RUN" -eq 0 ]; then
  pct exec "$NEXT_ID" -- sh -c \
    "umask 077; printf 'OPENCODE_PASSWORD=%s\nOPENCODE_SERVER_PASSWORD=%s\n' '$PW' '$PW' > /etc/opencode/serve.env"
fi

info "启用并启动 opencode-serve"
run pct exec "$NEXT_ID" -- systemctl enable --now opencode-serve

info "等待健康检查"
HEALTHY=0
for _ in $(seq 1 60); do
  if [ "$DRY_RUN" -eq 1 ]; then HEALTHY=1; break; fi
  if curl -fsS -u "opencode:$PW" "http://$IP:4096/global/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 2
done
if [ "$HEALTHY" -ne 1 ]; then
  echo "错误：opencode-serve 健康检查超时。已创建 VMID $NEXT_ID。" >&2
  echo "查看日志：pct exec $NEXT_ID -- journalctl -u opencode-serve -n 50 --no-pager" >&2
  echo "清理：pct stop $NEXT_ID && pct destroy $NEXT_ID" >&2
  exit 1
fi

# 模板里的代理配置随克隆带过来；这里只验证，不修改。
if [ "$DRY_RUN" -eq 0 ]; then
  info "验证出站代理"
  if pct exec "$NEXT_ID" -- test -f /etc/opencode/proxy.env 2>/dev/null; then
    if pct exec "$NEXT_ID" -- bash -lc \
      'curl -fsS --max-time 10 -o /dev/null https://api.github.com' 2>/dev/null; then
      echo "    出站代理正常"
    else
      echo "警告：出站代理不可用，opencode 可能无法访问模型服务。" >&2
      echo "      排查：pct exec $NEXT_ID -- bash -lc 'env | grep -i proxy'" >&2
      echo "            pct exec $NEXT_ID -- cat /etc/opencode/proxy.env" >&2
    fi
  else
    echo "警告：模板缺少 /etc/opencode/proxy.env，用户 LXC 将无法出站。" >&2
    echo "      按 docs/lxc-template-manual.md §1.4 重建模板。" >&2
  fi
fi

# --- 更新 users.json -------------------------------------------------------
info "更新 docker LXC 内的 users.json"
if [ "$DRY_RUN" -eq 0 ]; then
  TMP_USERS=$(mktemp /tmp/users.json.XXXXXX)
  if pct exec "$DOCKER_LXC" -- test -f "$CONFIG_PATH" 2>/dev/null; then
    pct exec "$DOCKER_LXC" -- cat "$CONFIG_PATH" > "$TMP_USERS"
  else
    echo "[]" > "$TMP_USERS"
  fi

  python3 - "$TMP_USERS" "$OPEN_ID" "$SLUG" "$IP" "$PW" <<'PY'
import json, sys

path, open_id, slug, ip, pw = sys.argv[1:6]
with open(path, encoding="utf-8") as handle:
    users = json.load(handle)
if not isinstance(users, list):
    sys.exit("users.json 顶层必须是数组")

users = [
    item for item in users
    if item.get("open_id") != open_id and item.get("slug") != slug
]
users.append({
    "open_id": open_id,
    "slug": slug,
    "url": f"http://{ip}:4096",
    "password": pw,
    "workspace": "/workspace",
})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(users, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

  # 先备份远端，再替换。pct push 会替换 inode，所以 users.json 必须挂目录。
  pct exec "$DOCKER_LXC" -- sh -c \
    "test -f '$CONFIG_PATH' && cp '$CONFIG_PATH' '$CONFIG_PATH.bak' || true"
  pct push "$DOCKER_LXC" "$TMP_USERS" "$CONFIG_PATH"
  # 容器内 bridge 以 UID 1000 运行，宿主机 oc 也是 1000；保持 600 权限。
  pct exec "$DOCKER_LXC" -- sh -c \
    "chown 1000:1000 '$CONFIG_PATH' && chmod 600 '$CONFIG_PATH'"
  rm -f "$TMP_USERS"
fi

# --- 冒烟 --------------------------------------------------------------
if [ "$SKIP_SMOKE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  info "执行冒烟测试"
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SMOKE="$SCRIPT_DIR/../../scripts/smoke-test.sh"
  if [ -x "$SMOKE" ]; then
    if ! "$SMOKE" --url "http://$IP:4096" --password "$PW"; then
      echo "错误：冒烟测试失败。LXC $NEXT_ID 已创建且 users.json 已更新。" >&2
      echo "排查后重试：$SMOKE --url http://$IP:4096 --password <pw>" >&2
      exit 1
    fi
  else
    echo "警告：未找到 $SMOKE，跳过冒烟测试" >&2
  fi
fi

echo
echo "用户 $SLUG 已开通："
echo "  VMID:   $NEXT_ID"
echo "  IP:     $IP"
echo "  URL:    http://$IP:4096"
echo "  open_id: $OPEN_ID"
echo "  密码已写入 /etc/opencode/serve.env（$PW）"
echo "users.json 已更新，bridge 会在 5 秒内热加载。"
