#!/bin/bash
#SBATCH --job-name=anuga-gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=1
#SBATCH --time=02:00:00
#SBATCH --output=anuga-%j.out

# Cluster runs Singularity 3.7.3. Some sites need a module load first:
#   module load singularity

SIF="$HOME/containers/anuga-gpu.sif"
ENVFILE="$HOME/containers/anuga.env"
SCRATCH="/scratch/$USER/anuga-$SLURM_JOB_ID"

mkdir -p "$SCRATCH/out" "$SCRATCH/tmp" "$SCRATCH/nv-cache"

# --nv        exposes the host NVIDIA driver + libs
# --cleanenv  drops your shell environment so only --env-file applies
# -B          binds writable scratch; the SIF itself is read-only squashfs
singularity run \
  --nv \
  --cleanenv \
  --env-file "$ENVFILE" \
  -B "$SCRATCH:/scratch" \
  "$SIF"

# --- Troubleshooting ---------------------------------------------------------
#
# Check the driver actually reaches the container:
#   singularity exec --nv "$SIF" nvidia-smi
#
# The image is built for cc86 (this A40) and cc120 (the dev laptop's 5060).
# Confirm the device the job actually got:
#   singularity exec --nv "$SIF" nvidia-smi --query-gpu=name,compute_cap --format=csv
#   singularity exec --nv "$SIF" cat /opt/build-info.txt
#
# THE LIKELY FAILURE HERE: the image is built against CUDA 12.8 (needed for the
# laptop's Blackwell GPU), but this cluster's driver may be older. CUDA minor
# version compatibility usually carries a 12.x binary on any >=525 driver.
# Check what you have:
#   nvidia-smi --query-gpu=driver_version --format=csv
# If you hit "CUDA driver version is insufficient", the fix is a cluster-only
# rebuild pinned to the older toolkit, dropping Blackwell:
#   NVHPC_TAG=24.7-devel-cuda12.5-ubuntu22.04 GPU_ARCH=cc86 ./build-sif.sh
#
# Confirm the GPU extension loads on a real device:
#   singularity exec --nv "$SIF" python -c \
#     "from anuga.shallow_water import sw_domain_gpu_ext; print('ok')"
#
# Trace whether offload is happening at all: set NVCOMPILER_ACC_NOTIFY=3
# in anuga.env and watch for kernel launch lines.
