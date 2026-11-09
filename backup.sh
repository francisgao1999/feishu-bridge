#!/usr/bin/env bash
set -euo pipefail

BASE=/home/oc/feishu-opencode-bridge
DEST=$BASE/backups
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$DEST"

python3 - "$BASE/bridge.db" "$TMP/bridge.db" <<'PY'
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

cp "$BASE/bridge.env" "$TMP/bridge.env"
mkdir -p "$TMP/app"
cp "$BASE"/app/*.py "$TMP/app/"

tar -czf "$DEST/bridge-$STAMP.tar.gz" -C "$TMP" .
chmod 600 "$DEST/bridge-$STAMP.tar.gz"

find "$DEST" -name 'bridge-*.tar.gz' -mtime +7 -delete

echo "backup written: $DEST/bridge-$STAMP.tar.gz"
