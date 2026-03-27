#!/usr/bin/env bash

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-music-analysis-rg}"
LOCATION="${LOCATION:-eastus}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-music-analysis-env}"
ACR_NAME="${ACR_NAME:-musicanalysisacr}"
WORKLOAD_PROFILE_NAME="${WORKLOAD_PROFILE_NAME:-gpu-t4}"
WORKLOAD_PROFILE_TYPE="${WORKLOAD_PROFILE_TYPE:-Consumption-GPU-NC8as-T4}"
CONTAINER_APP_NAME="${CONTAINER_APP_NAME:?Set CONTAINER_APP_NAME}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:?Set CONTAINER_IMAGE}"
TARGET_PORT="${TARGET_PORT:-5000}"
CPU="${CPU:-8.0}"
MEMORY="${MEMORY:-56Gi}"
MIN_REPLICAS="${MIN_REPLICAS:-1}"
MAX_REPLICAS="${MAX_REPLICAS:-1}"

echo "Ensuring resource group ${RESOURCE_GROUP} exists"
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --query "properties.provisioningState" \
  -o tsv >/dev/null

if ! az containerapp env show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${ENVIRONMENT_NAME}" \
  >/dev/null 2>&1; then
  echo "Creating Container Apps environment ${ENVIRONMENT_NAME}"
  az containerapp env create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ENVIRONMENT_NAME}" \
    --location "${LOCATION}" \
    --query "properties.provisioningState" \
    -o tsv >/dev/null
fi

if ! az containerapp env workload-profile show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${ENVIRONMENT_NAME}" \
  --workload-profile-name "${WORKLOAD_PROFILE_NAME}" \
  >/dev/null 2>&1; then
  echo "Adding workload profile ${WORKLOAD_PROFILE_NAME} (${WORKLOAD_PROFILE_TYPE})"
  az containerapp env workload-profile add \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ENVIRONMENT_NAME}" \
    --workload-profile-name "${WORKLOAD_PROFILE_NAME}" \
    --workload-profile-type "${WORKLOAD_PROFILE_TYPE}" \
    -o none
fi

registry_server="$(az acr show --name "${ACR_NAME}" --query loginServer -o tsv)"
registry_username="$(az acr credential show --name "${ACR_NAME}" --query username -o tsv)"
registry_password="$(az acr credential show --name "${ACR_NAME}" --query passwords[0].value -o tsv)"

if az containerapp show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${CONTAINER_APP_NAME}" \
  >/dev/null 2>&1; then
  echo "Updating container app ${CONTAINER_APP_NAME}"
  az containerapp update \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${CONTAINER_APP_NAME}" \
    --image "${CONTAINER_IMAGE}" \
    --cpu "${CPU}" \
    --memory "${MEMORY}" \
    --min-replicas "${MIN_REPLICAS}" \
    --max-replicas "${MAX_REPLICAS}" \
    --workload-profile-name "${WORKLOAD_PROFILE_NAME}" \
    --set-env-vars \
      COG_NO_UPDATE_CHECK=1 \
      PYTHONUNBUFFERED=1 \
      AIO_MAX_QUEUE=16 \
      AIO_JOB_RETENTION_SECONDS=86400 \
    -o none
else
  echo "Creating container app ${CONTAINER_APP_NAME}"
  az containerapp create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${CONTAINER_APP_NAME}" \
    --environment "${ENVIRONMENT_NAME}" \
    --image "${CONTAINER_IMAGE}" \
    --ingress external \
    --target-port "${TARGET_PORT}" \
    --cpu "${CPU}" \
    --memory "${MEMORY}" \
    --min-replicas "${MIN_REPLICAS}" \
    --max-replicas "${MAX_REPLICAS}" \
    --workload-profile-name "${WORKLOAD_PROFILE_NAME}" \
    --registry-server "${registry_server}" \
    --registry-username "${registry_username}" \
    --registry-password "${registry_password}" \
    --env-vars \
      COG_NO_UPDATE_CHECK=1 \
      PYTHONUNBUFFERED=1 \
      AIO_MAX_QUEUE=16 \
      AIO_JOB_RETENTION_SECONDS=86400 \
    -o none
fi

fqdn="$(az containerapp show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${CONTAINER_APP_NAME}" \
  --query properties.configuration.ingress.fqdn \
  -o tsv)"

echo "https://${fqdn}"
