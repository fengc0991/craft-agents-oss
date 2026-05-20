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
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.StringParameterDefinition>
          <name>IMAGE_REGISTRY</name>
          <description>Image repository without tag</description>
          <defaultValue>ops-harbor.rabyte.cn/rabyte-data/rabyte-pre/pre/craft-agents-paiwork</defaultValue>
          <trim>true</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>IMAGE_TAG</name>
          <description>Optional image tag. Defaults to BUILD_NUMBER</description>
          <defaultValue></defaultValue>
          <trim>true</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.PasswordParameterDefinition>
          <name>CRAFT_WEBUI_PASSWORD</name>
          <description>Required WebUI login password</description>
          <defaultValue></defaultValue>
        </hudson.model.PasswordParameterDefinition>
        <hudson.model.PasswordParameterDefinition>
          <name>CRAFT_SERVER_TOKEN</name>
          <description>Optional server token. Leave empty to auto-generate</description>
          <defaultValue></defaultValue>
        </hudson.model.PasswordParameterDefinition>
        <hudson.model.PasswordParameterDefinition>
          <name>RABYTE_LLM_API_KEY</name>
          <description>Optional Rabyte LLM API key</description>
          <defaultValue></defaultValue>
        </hudson.model.PasswordParameterDefinition>
        <hudson.model.PasswordParameterDefinition>
          <name>SEALOS_LLM_API_KEY</name>
          <description>Optional Sealos LLM API key</description>
          <defaultValue></defaultValue>
        </hudson.model.PasswordParameterDefinition>
        <hudson.model.PasswordParameterDefinition>
          <name>PAI_OBS_API_KEY</name>
          <description>Optional PaiWork observability API key</description>
          <defaultValue></defaultValue>
        </hudson.model.PasswordParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>PAI_OBS_BASE_URL</name>
          <description>PaiWork observability gateway URL</description>
          <defaultValue>http://192.168.15.57:30100</defaultValue>
          <trim>true</trim>
        </hudson.model.StringParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>
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
