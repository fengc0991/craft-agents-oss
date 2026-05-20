#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE_HOME="${SOURCE_HOME:-/root/.craft-agent}"
DEST="$ROOT/deploy/paiwork/seed/.craft-agent"
WORKSPACE_SLUG="${WORKSPACE_SLUG:-paiwork}"

if [ ! -f "$SOURCE_HOME/config.json" ]; then
  echo "Missing source config: $SOURCE_HOME/config.json" >&2
  exit 1
fi

if [ ! -d "$SOURCE_HOME/workspaces/$WORKSPACE_SLUG" ]; then
  echo "Missing source workspace: $SOURCE_HOME/workspaces/$WORKSPACE_SLUG" >&2
  exit 1
fi

mkdir -p "$DEST/workspaces/$WORKSPACE_SLUG"

jq --arg slug "$WORKSPACE_SLUG" '
  .activeWorkspaceId = (.workspaces[] | select(.slug == $slug) | .id)
  | .activeSessionId = null
  | .workspaces = [.workspaces[] | select(.slug == $slug)]
' "$SOURCE_HOME/config.json" > "$DEST/config.json"

rsync -a --delete \
  --exclude 'skills/paiwork-observability/.paiobs.env' \
  --exclude 'skills/paiwork-observability/.local-lark-cli/node_modules' \
  --exclude 'skills/paiwork-observability/.local-lark-cli/lib/node_modules' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.server.lock' \
  --exclude 'credentials.enc' \
  "$SOURCE_HOME/workspaces/$WORKSPACE_SLUG/" \
  "$DEST/workspaces/$WORKSPACE_SLUG/"

find "$DEST" -type f \( -name '.paiobs.env' -o -name 'credentials.enc' -o -name '.server.lock' -o -name '*.pyc' \) -delete
find "$DEST" -type d \( -name node_modules -o -name __pycache__ \) -prune -exec rm -rf {} +

echo "Seed ready: $DEST"
jq '{activeWorkspaceId, activeSessionId, workspaces}' "$DEST/config.json"
