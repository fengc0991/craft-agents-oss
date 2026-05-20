#!/usr/bin/env bash
set -euo pipefail

JENKINS_URL="${JENKINS_URL:-https://ops-jenkins.rabyte.cn}"
JOB_NAME="${JOB_NAME:-test-craft-agents-paiwork}"

: "${JENKINS_USER:?Set JENKINS_USER}"
: "${JENKINS_PASSWORD:?Set JENKINS_PASSWORD or an API token}"

tmp_cookie="$(mktemp)"
cleanup() {
  rm -f "$tmp_cookie"
}
trap cleanup EXIT

crumb_json="$(curl -fsS -u "$JENKINS_USER:$JENKINS_PASSWORD" -c "$tmp_cookie" \
  "$JENKINS_URL/crumbIssuer/api/json")"
crumb_field="$(printf '%s' "$crumb_json" | jq -r '.crumbRequestField')"
crumb="$(printf '%s' "$crumb_json" | jq -r '.crumb')"

curl -fsS -u "$JENKINS_USER:$JENKINS_PASSWORD" -b "$tmp_cookie" \
  -H "$crumb_field: $crumb" \
  -X POST \
  "$JENKINS_URL/job/$JOB_NAME/build" >/dev/null

echo "Triggered Jenkins job: $JENKINS_URL/job/$JOB_NAME/"
