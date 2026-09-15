#!/usr/bin/env bash
# Build once for both GPUs, test locally on the 5060, then ship to the A40 cluster.
#
#   nix-shell -p singularity squashfsTools
#   ./build-sif.sh
#
# Native mode only. Never add --oci: OCI-SIF is unreadable by Singularity 3.7.3.
set -euo pipefail

NVHPC_TAG="${NVHPC_TAG:-24.7-devel-cuda_multi-ubuntu22.04}"
# cc80 (A30) is NOT optional — vrhpc4 carries one and Slurm advertises a bare
# "gpu:8", so a job cannot request a card type. A cc86-only binary imports fine
# and then dies at the first kernel launch. Keep in step with
# DockerfileSingularity's ARG GPU_ARCH.
GPU_ARCH="${GPU_ARCH:-cc80,cc86,cc89}"
IMAGE_TAG="anuga-gpu:latest"
SIF_OUT="anuga-gpu.sif"

# nvhpc devel unpacks to tens of GB and the SIF conversion needs comparable
# scratch. /tmp is tmpfs on many distros and will OOM: use real disk.
export SINGULARITY_TMPDIR="${SINGULARITY_TMPDIR:-$PWD/.sing-tmp}"
export SINGULARITY_CACHEDIR="${SINGULARITY_CACHEDIR:-$PWD/.sing-cache}"
mkdir -p "$SINGULARITY_TMPDIR" "$SINGULARITY_CACHEDIR"
df -h "$SINGULARITY_TMPDIR" | tail -1

echo "==> Docker build  (nvhpc=$NVHPC_TAG  arch=$GPU_ARCH)"
docker build \
  --build-arg "NVHPC_TAG=$NVHPC_TAG" \
  --build-arg "GPU_ARCH=$GPU_ARCH" \
  -f DockerfileSingularity \
  -t "$IMAGE_TAG" .

echo
echo "==> Local GPU test on the 5060, in Docker (fast iteration, no SIF needed)"
docker run --rm --gpus all "$IMAGE_TAG" \
  bash -c 'nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader; \
           python -c "from anuga.shallow_water import sw_domain_gpu_ext; print(\"gpu ext OK\")"'

echo
echo "==> Converting to SIF (native mode)"
singularity build "$SIF_OUT" "docker-daemon://${IMAGE_TAG}"

echo "==> Confirming classic SIF, not OCI-SIF"
singularity sif list "$SIF_OUT"
# Expect a squashfs data partition. Any mention of OCI blobs means OCI mode
# got enabled somewhere and 3.7.3 will refuse the image.

echo
echo "==> Same test again, this time through Singularity on the 5060"
singularity exec --nv "$SIF_OUT" \
  python -c "from anuga.shallow_water import sw_domain_gpu_ext; print('gpu ext OK via singularity')"

ls -lh "$SIF_OUT"
cat <<'NEXT'

If that passed on the 5060, the identical binary already contains cc86 cubins
for the A40. Ship it:

  rsync -avP --partial anuga-gpu.sif anuga.env run-anuga.slurm \
    jmormal@upvnet.upv.es@vrhpcadm1:~/containers/
NEXT
