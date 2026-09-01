#!/usr/bin/env bash
# Drain jobs:hpc with the SIF, reaching Redis and Postgres over the TAILNET.
#
# Same worker as run-local-worker.sh, but pointed at the tailnet names the real
# HPC worker.env uses (hidris-redis / hidris-db) instead of kubectl
# port-forwards. This is the script that answers "does the tailscale path
# actually carry a simulation", rather than only proving the queue wiring.
#
# What this does and does NOT cover:
#
#   covered      libpq and redis over tailscale, at whatever path tailscale
#                negotiates (direct or DERP) — including the 666MB storm-cube
#                large-object read, which is the transfer that matters.
#   NOT covered  the netns transport (unshare + slirp4netns + its own
#                tailscaled) that hpc-entrypoint.sh sets up on a compute node.
#                This uses the HOST's tailscale0. A compute node has no
#                CAP_NET_ADMIN, which is why it needs the netns dance at all.
#                Reaching the DB from inside that netns is a strictly harder
#                problem — it deadlocked mid storm-cube read on 2026-08-08 —
#                so a pass here is necessary but not sufficient for the cluster.
#
#   ./run-local-worker-tailnet.sh
#   ANUGA_GPU_MODE=2 ./run-local-worker-tailnet.sh
#
# Credentials come from services/hpc-probe/probe.env when present (the same
# PG_PASSWORD the cluster uses), otherwise from the in-cluster secret.
set -uo pipefail

cd "$(dirname "$0")"
SIF="$PWD/anuga-gpu.sif"
WORKDIR="${WORKDIR:-$PWD/.local-run-tailnet}"
QUEUE="${QUEUE:-jobs:hpc}"
REDIS_HOST="${REDIS_HOST:-hidris-redis}"
DB_HOST="${DB_HOST:-hidris-db}"
GPU_MODE="${ANUGA_GPU_MODE:-1}"
DRV="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"

[[ -r "$SIF" ]] || { echo "missing SIF: $SIF" >&2; exit 2; }

# --- preflight: the tailnet, not the code, is what this script is testing ----
command -v tailscale >/dev/null || { echo "tailscale not on PATH" >&2; exit 2; }
for h in "$REDIS_HOST" "$DB_HOST"; do
  if ! getent hosts "$h" >/dev/null 2>&1; then
    echo "!! $h does not resolve — is MagicDNS on? (tailscale status)" >&2
    exit 3
  fi
done

echo "==> tailnet path check (DERP means every byte is relayed):"
for h in "$REDIS_HOST" "$DB_HOST"; do
  # Captured rather than piped: `tailscale ping` exits non-zero whenever it
  # could not get a DIRECT path, even when the peer answered fine over DERP,
  # and under `pipefail` that would mislabel a working host as unreachable.
  out="$(timeout 25 tailscale ping --c 1 "$h" 2>&1 | head -1)"
  printf '    %-14s %s\n' "$h" "${out:-no answer}"
done

# --- credentials -------------------------------------------------------------
PROBE=../hpc-probe/probe.env
if [[ -r "$PROBE" ]]; then
  PGUSER="$(grep -E '^PG_USER=' "$PROBE" | cut -d= -f2-)"
  PGPW="$(grep -E '^PG_PASSWORD=' "$PROBE" | cut -d= -f2-)"
  DBNAME="$(grep -E '^DB_NAME=' "$PROBE" | cut -d= -f2-)"
  echo "==> credentials from $PROBE (user=$PGUSER)"
else
  PGUSER="$(kubectl get secret hidris-db-app -o jsonpath='{.data.username}' | base64 -d)"
  PGPW="$(kubectl get secret hidris-db-app -o jsonpath='{.data.password}' | base64 -d)"
  DBNAME=hidris
  echo "==> credentials from the hidris-db-app secret (user=$PGUSER)"
fi
[[ -n "$PGPW" ]] || { echo "no PG_PASSWORD available" >&2; exit 2; }

# tasks.py resolves the DEM relative to CWD and writes region.asc beside it, so
# the CWD must contain src/ AND be writable — the image root is read-only
# squashfs. Same trick as hpc_run.py:prepare_workdir.
mkdir -p "$WORKDIR/tmp"
[[ -L "$WORKDIR/src" ]] || ln -s /app/src "$WORKDIR/src"

ENVFILE="$WORKDIR/worker.env"
umask 077
cat > "$ENVFILE" <<EOF
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
OMP_NUM_THREADS=1
QUEUE=$QUEUE
REDIS_URL=redis://$REDIS_HOST:6379
DB_HOST=$DB_HOST
DB_PORT=5432
DB_NAME=$DBNAME
PG_USER=$PGUSER
PG_PASSWORD=$PGPW
ANUGA_ELEVATION=/app/src/MDT_malla_5m_etrs89h30.ers
ANUGA_GPU_SUBPROCESS=0
ANUGA_GPU_MODE=$GPU_MODE
ANUGA_YIELDSTEP=${ANUGA_YIELDSTEP:-20}
TMPDIR=/scratch/tmp
CUDA_CACHE_PATH=/scratch/nv-cache
EOF

# --nv is unusable here: no system singularity, and nix's build hardcodes an
# ldconfig with no ld.so.cache, so it finds no GPU libraries. Bind the driver
# explicitly instead. The cluster's singularity is fine — do not change
# run-simulation.slurm on account of this.
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
  echo "!! no host driver libs for '$DRV' — running without GPU binds" >&2
fi

echo "==> queue=$QUEUE  gpu_mode=$GPU_MODE  redis=$REDIS_HOST  db=$DB_HOST"
echo "==> submit with the HPC toggle in the UI; Ctrl-C to stop"

# --cleanenv so only the env-file applies; /etc/resolv.conf is bound from the
# host, which is what makes the MagicDNS names above resolve inside.
exec nix-shell -p singularity --run "singularity exec --cleanenv \
  ${NVBIND[*]} \
  --env-file '$ENVFILE' \
  -B '$WORKDIR:/scratch' \
  --pwd /scratch \
  '$SIF' python -u /app/src/worker.py"
