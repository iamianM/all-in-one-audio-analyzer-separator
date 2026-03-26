# Deployment

This repo is deployed to Azure Container Apps as a separate GPU-backed inference
service. The goal is to run the upstream Cog model as close to the hosted
Replicate version as possible.

## Branch targets

- `dev` deploys to `music-analysis-aio-dev`
- `main` deploys to `music-analysis-aio`

## Azure resources

- Resource group: `music-analysis-rg`
- Container Apps environment: `music-analysis-env`
- ACR: `musicanalysisacr`
- ACR repository: `music-analysis-aio`
- GPU workload profile: `gpu-t4` (`Consumption-GPU-NC8as-T4`)

## First-time setup

1. Create repo variables in GitHub:
   - `AZURE_CLIENT_ID`
   - `AZURE_TENANT_ID`
   - `AZURE_SUBSCRIPTION_ID`
2. Add Azure AD federated credentials for this repo and both branches:
   - `repo:iamianM/all-in-one-audio-analyzer-separator:ref:refs/heads/main`
   - `repo:iamianM/all-in-one-audio-analyzer-separator:ref:refs/heads/dev`
3. Push to `dev` or `main`.

## Manual deployment

Build the image in Azure Container Registry, then create or update the target
app:

```bash
az acr build \
  --registry musicanalysisacr \
  --image music-analysis-aio:manual-test \
  --file Dockerfile.azure \
  .

CONTAINER_APP_NAME=music-analysis-aio-dev \
CONTAINER_IMAGE=musicanalysisacr.azurecr.io/music-analysis-aio:manual-test \
./scripts/bootstrap_azure.sh
```

The deployed Cog server should expose `POST /predictions` on port `5000`. This
image build path does not generate `.cog/openapi_schema.json`, so the workflow
verifies startup via `GET /` instead of `GET /openapi.json`.
