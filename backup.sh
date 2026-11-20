#!/usr/bin/env bash
set -euo pipefail

BASE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

if [[ -f "$BASE/bridge.env" ]]; then
    # shellcheck disable=SC1091
    source "$BASE/bridge.env"
fi

resolve_path() {
    local value=$1
    local base=$2
    case "$value" in
        "~") value=$HOME ;;
        "~/"*) value=$HOME/${value#\~/} ;;
    esac
    if [[ "$value" != /* ]]; then
        value="$base/$value"
    fi
    printf '%s\n' "$value"
}

DATA_DIR=$(resolve_path "${BRIDGE_DATA_DIR:-$BASE}" "$BASE")
DB=$(resolve_path "${BRIDGE_DB:-$DATA_DIR/bridge.db}" "$DATA_DIR")
ENV_FILE=$(resolve_path "${BRIDGE_ENV_FILE:-$BASE/bridge.env}" "$BASE")
USERS_FILE="${BRIDGE_USERS_FILE:-$BASE/config/users.json}"
# bridge.env（compose 用）里写的是容器内路径 /app/...；宿主机备份时映射回安装目录。
case "$USERS_FILE" in
    /app/*) USERS_FILE="$BASE/${USERS_FILE#/app/}" ;;
esac
USERS_FILE=$(resolve_path "$USERS_FILE" "$BASE")
DEST=$(resolve_path "${BRIDGE_BACKUP_DIR:-$BASE/backups}" "$BASE")
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

umask 077
mkdir -p "$DEST"

if [[ ! -f "$DB" ]]; then
    echo "missing database: $DB" >&2
    exit 1
fi

PYTHON=${PYTHON:-$BASE/.venv/bin/python}
if [[ ! -x "$PYTHON" ]]; then
    PYTHON=python3
fi

"$PYTHON" - "$DB" "$TMP/bridge.db" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY

if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$TMP/bridge.env"
else
    echo "warning: missing env file: $ENV_FILE" >&2
fi

if [[ -f "$USERS_FILE" ]]; then
    mkdir -p "$TMP/config"
    cp "$USERS_FILE" "$TMP/config/users.json"
else
    echo "warning: missing users file: $USERS_FILE" >&2
fi

mkdir -p "$TMP/app"
cp -r "$BASE/app/." "$TMP/app/"
rm -rf "$TMP/app/__pycache__"

tar -czf "$DEST/bridge-$STAMP.tar.gz" -C "$TMP" .
chmod 600 "$DEST/bridge-$STAMP.tar.gz"

find "$DEST" -name 'bridge-*.tar.gz' -mtime +7 -delete

echo "backup written: $DEST/bridge-$STAMP.tar.gz"
