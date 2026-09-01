#!/usr/bin/env bash
# Drain the HPC queue with the SIF, on this laptop.
#
# Purpose: test the whole HPC path without the 10-15GB upload to vrhpcadm1.
# Pick "HPC" in the solve toggle and the API enqueues onto jobs:hpc (rather
# than sbatch'ing, which is now target="slurm"); this script runs the SIF's RQ
# worker against that queue, so the job is picked up here. Progress publishes to
# sim:events:{job_id} exactly as in production, so the frontend is unchanged.
#
# jobs:hpc is deliberately NOT jobs:gpu: KEDA's worker-gpu ScaledJob drains that
# one, and the two would race for the same job.
#
# Runs the SIF's RQ worker (src/worker.py) rather than the Slurm entrypoint
# (hpc_run.py), because the entrypoint solves one instance named in $PUBLIC_ID
# and exits — it has no queue to poll. See DockerfileSingularity:137.
#
#   ./run-local-worker.sh              # CPU solve (default; see GPU note)
#   ANUGA_GPU_MODE=2 ./run-local-worker.sh
#
# Ctrl-C stops it. Two local deviations from the cluster, both host-specific:
#
#  1. Redis/Postgres come over kubectl port-forward, not the tailnet. The
#     in-cluster names (redis, hidris-db-rw) don't resolve here, and the tailnet
#     route to hidris-redis is DERP-relayed at ~400ms, so localhost is both
#     simpler and far faster.
#  2. --nv is replaced by explicit driver binds: this box has no system
#     singularity, and nix's build hardcodes an ldconfig with no ld.so.cache, so
#     --nv finds no GPU libraries. Cluster singularity is fine; leave
#     run-simulation.slurm alone.
set -uo pipefail

cd "$(dirname "$0")"
SIF="$PWD/anuga-gpu.sif"
WORKDIR="${WORKDIR:-$PWD/.local-run}"
QUEUE="${QUEUE:-jobs:hpc}"
REDIS_PORT="${REDIS_PORT:-16379}"
DB_PORT_LOCAL="${DB_PORT_LOCAL:-15432}"

# ANUGA's GPU kernels are built for cc86,cc89 (DockerfileSingularity:44). If
# this host's GPU isn't one of those, mode 2 has no cubin to launch, so default
# to the CPU multiprocessor mode and let it be overridden explicitly.
GPU_MODE="${ANUGA_GPU_MODE:-1}"
DRV="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"

[[ -r "$SIF" ]] || { echo "missing SIF: $SIF" >&2; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl not on PATH" >&2; exit 2; }

# tasks.py resolves the DEM as "./src/MDT_malla_5m_etrs89h30.ers" and writes
# "region.asc" next to it, so the CWD must contain a src/ AND be writable. The
# image root is read-only squashfs, so we run from bind-mounted scratch with
# src symlinked in — the same trick hpc_run.py:prepare_workdir does for Slurm.
mkdir -p "$WORKDIR/tmp"
[[ -L "$WORKDIR/src" ]] || ln -s /app/src "$WORKDIR/src"

PGPW="$(kubectl get secret hidris-db-app -o jsonpath='{.data.password}' | base64 -d)"
PGUSER="$(kubectl get secret hidris-db-app -o jsonpath='{.data.username}' | base64 -d)"
[[ -n "$PGPW" ]] || { echo "could not read hidris-db-app secret" >&2; exit 2; }

kubectl port-forward svc/redis "$REDIS_PORT:6379" >"$WORKDIR/pf-redis.log" 2>&1 &
PF_REDIS=$!
kubectl port-forward svc/hidris-db-rw "$DB_PORT_LOCAL:5432" >"$WORKDIR/pf-db.log" 2>&1 &
PF_DB=$!
trap 'kill $PF_REDIS $PF_DB 2>/dev/null' EXIT INT TERM

for _ in $(seq 1 20); do
  (exec 3<>/dev/tcp/127.0.0.1/"$REDIS_PORT") 2>/dev/null && break
  sleep 0.5
done

ENVFILE="$WORKDIR/worker.env"
umask 077
cat > "$ENVFILE" <<EOF
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
OMP_NUM_THREADS=1
QUEUE=$QUEUE
REDIS_URL=redis://127.0.0.1:$REDIS_PORT
DB_HOST=127.0.0.1
DB_PORT=$DB_PORT_LOCAL
DB_NAME=hidris
PG_USER=$PGUSER
PG_PASSWORD=$PGPW
ANUGA_ELEVATION=/app/src/MDT_malla_5m_etrs89h30.ers
ANUGA_GPU_SUBPROCESS=0
ANUGA_GPU_MODE=$GPU_MODE
ANUGA_YIELDSTEP=${ANUGA_YIELDSTEP:-20}
TMPDIR=/scratch/tmp
CUDA_CACHE_PATH=/scratch/nv-cache
EOF

NVBIND=()
if [[ -n "$DRV" && -r "/usr/lib/libcuda.so.$DRV" ]]; then
  NVBIND=(
    -B "/usr/bin/nvidia-smi:/usr/local/bin/nvidia-smi"
    -B "/usr/lib/libcuda.so.$DRV:/usr/lib/x86_64-linux-gnu/libcuda.so.1"
    -B "/usr/lib/libnvidia-ml.so.$DRV:/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1"
    -B "/usr/lib/libnvidia-ptxjitcompiler.so.$DRV:/usr/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1"
    -B "/usr/lib/libnvidia-nvvm.so.$DRV:/usr/lib/x86_64-linux-gnu/libnvidia-nvvm.so.4"
  )
else
  echo "!! no host driver libs found for '$DRV' — running without GPU binds" >&2
fi

echo "==> queue=$QUEUE  gpu_mode=$GPU_MODE  workdir=$WORKDIR"
echo "==> submit with the HPC toggle in the UI; Ctrl-C to stop"

exec nix-shell -p singularity --run "singularity exec --cleanenv \
  ${NVBIND[*]} \
  --env-file '$ENVFILE' \
  -B '$WORKDIR:/scratch' \
  --pwd /scratch \
  '$SIF' python -u /app/src/worker.py"
