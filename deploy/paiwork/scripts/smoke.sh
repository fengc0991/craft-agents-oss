#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:?Set BASE_URL, for example BASE_URL=http://192.168.x.x:30101}"

curl -fsS "$BASE_URL/health"
echo
echo "OK: $BASE_URL"
