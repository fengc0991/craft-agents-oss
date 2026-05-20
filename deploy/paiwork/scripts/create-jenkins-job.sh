#!/usr/bin/env bash
set -euo pipefail

JENKINS_URL="${JENKINS_URL:-https://ops-jenkins.rabyte.cn}"
JOB_NAME="${JOB_NAME:-test-craft-agents-paiwork}"
GIT_URL="${GIT_URL:-https://gitlab2.rabyte.cn/gl/fengc01/craft-agents-paiwork.git}"
GIT_BRANCH="${GIT_BRANCH:-*/main}"
SCRIPT_PATH="${SCRIPT_PATH:-deploy/paiwork/Jenkinsfile}"
GIT_CREDENTIALS_ID="${GIT_CREDENTIALS_ID:-jenkins_tag}"

: "${JENKINS_USER:?Set JENKINS_USER}"
: "${JENKINS_PASSWORD:?Set JENKINS_PASSWORD or an API token}"

tmp_cookie="$(mktemp)"
tmp_xml="$(mktemp)"
cleanup() {
  rm -f "$tmp_cookie" "$tmp_xml"
}
trap cleanup EXIT

crumb_json="$(curl -fsS -u "$JENKINS_USER:$JENKINS_PASSWORD" -c "$tmp_cookie" \
  "$JENKINS_URL/crumbIssuer/api/json")"
crumb_field="$(printf '%s' "$crumb_json" | jq -r '.crumbRequestField')"
crumb="$(printf '%s' "$crumb_json" | jq -r '.crumb')"

cat > "$tmp_xml" <<XML
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <description>Craft Agents PaiWork WebUI deployment. Managed by deploy/paiwork/scripts/create-jenkins-job.sh.</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty>
      <abortPrevious>false</abortPrevious>
    </org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>${GIT_URL}</url>
          <credentialsId>${GIT_CREDENTIALS_ID}</credentialsId>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches>
        <hudson.plugins.git.BranchSpec>
          <name>${GIT_BRANCH}</name>
        </hudson.plugins.git.BranchSpec>
      </branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions/>
    </scm>
    <scriptPath>${SCRIPT_PATH}</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
XML

job_url="$JENKINS_URL/job/$JOB_NAME"
status="$(curl -sS -o /dev/null -w '%{http_code}' -u "$JENKINS_USER:$JENKINS_PASSWORD" "$job_url/api/json")"

if [ "$status" = "200" ]; then
  curl -fsS -u "$JENKINS_USER:$JENKINS_PASSWORD" -b "$tmp_cookie" \
    -H "$crumb_field: $crumb" \
    -H 'Content-Type: application/xml' \
    --data-binary @"$tmp_xml" \
    "$job_url/config.xml" >/dev/null
  echo "Updated Jenkins job: $job_url"
else
  curl -fsS -u "$JENKINS_USER:$JENKINS_PASSWORD" -b "$tmp_cookie" \
    -H "$crumb_field: $crumb" \
    -H 'Content-Type: application/xml' \
    --data-binary @"$tmp_xml" \
    "$JENKINS_URL/createItem?name=$JOB_NAME" >/dev/null
  echo "Created Jenkins job: $job_url"
fi
