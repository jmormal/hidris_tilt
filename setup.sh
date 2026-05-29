#!/bin/bash
set -e
CLUSTER_NAME="hidris"
REGISTRY_NAME="dev-registry"
REGISTRY_PORT="5000"
IMAGE_TAG="k3d-${REGISTRY_NAME}:${REGISTRY_PORT}/k3s-cuda:v1.0.0"
IMAGE_TAG="k3d-${REGISTRY_NAME}.localhost:${REGISTRY_PORT}/k3s-cuda:v1.0.0"
# 1. Create a local registry if it doesn't exist
if [ "$(k3d registry list | grep ${REGISTRY_NAME})" == "" ]; then
  k3d registry create "${REGISTRY_NAME}" --port "${REGISTRY_PORT}"
fi

# 2. Build and push the custom CUDA + K3s image
docker build -t "${IMAGE_TAG}" -f Dockerfile .
docker push "${IMAGE_TAG}"

# 3. Create cluster using the custom image
k3d cluster create "${CLUSTER_NAME}" \
  --port "80:80@server:0" \
  --port "443:443@server:0" \
  --registry-use "k3d-${REGISTRY_NAME}:${REGISTRY_PORT}" \
  --image "${IMAGE_TAG}" \
  --gpus 1 \
  --volume "$(pwd)/services/jupyter/notebooks:/mnt/notebooks@server:0"

k3d kubeconfig merge "${CLUSTER_NAME}" --kubeconfig-merge-default
