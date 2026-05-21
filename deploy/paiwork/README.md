# PaiWork Intranet Deployment

This deploys Craft Agents WebUI/server with only the `paiwork` workspace exposed.
The Kubernetes Service uses NodePort `30102`.
The PaiWork manifest sets `CRAFT_WEBUI_DISABLE_AUTH=true`, so opening the
NodePort goes directly to the workspace UI without a WebUI login prompt.

## Local Build

```bash
bash deploy/paiwork/scripts/prepare-seed.sh
IMAGE=craft-agents-paiwork:local bash deploy/paiwork/scripts/build-image.sh
```

## Kubernetes/Rancher Deploy

Use a registry image that Rancher nodes can pull:

```bash
export IMAGE=<registry>/craft-agents-paiwork:<tag>
export K8S_CONTEXT=test-saas-acs-new
export CRAFT_SERVER_TOKEN=<long-random-token>
export CRAFT_WEBUI_PASSWORD=<login-password>
export RABYTE_LLM_API_KEY=<llm-key>
export PAI_OBS_API_KEY=<paiwork-observability-key>
export PAI_OBS_BASE_URL=http://192.168.15.57:30100

bash deploy/paiwork/scripts/deploy-k8s.sh
```

Default Kubernetes context is `test-saas-acs-new`; the scripts explicitly switch
to it before applying manifests. Default namespace is `rabyte-data-pre-data`,
matching the existing Pai Automation deployment.

The Jenkins pipeline also creates or updates Kubernetes Secret
`craft-agents-paiwork-secret` automatically during the Deploy stage. In
**Build with Parameters**, fill at least:

```text
CRAFT_WEBUI_PASSWORD
```

Leave `CRAFT_SERVER_TOKEN` empty to let Jenkins generate a strong token.
`RABYTE_LLM_API_KEY`, `SEALOS_LLM_API_KEY`, and `PAI_OBS_API_KEY` are optional.

The rendered manifest is written to:

```text
deploy/paiwork/k8s/rendered.yaml
```

Health check:

```bash
BASE_URL=http://<intranet-node-ip>:30102 bash deploy/paiwork/scripts/smoke.sh
```

## Jenkins API Automation

After pushing this branch to a Git remote Jenkins can read:

```bash
export JENKINS_USER=<user>
export JENKINS_PASSWORD=<password-or-api-token>
export JOB_NAME=test-craft-agents-paiwork
export GIT_URL=https://gitlab2.rabyte.cn/gl/fengc01/craft-agents-paiwork.git
export GIT_BRANCH='*/main'

bash deploy/paiwork/scripts/create-jenkins-job.sh
bash deploy/paiwork/scripts/trigger-jenkins-job.sh
```

## Notes

- The seed intentionally excludes `fengchao`, `.paiobs.env`, `credentials.enc`, and `.server.lock`.
- Runtime secrets are stored in Kubernetes Secret `craft-agents-paiwork-secret`.
- On first boot, the PVC is initialized from `/seed/.craft-agent`; existing PVC data is kept.
