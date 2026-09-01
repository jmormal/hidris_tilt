#!/bin/bash
#SBATCH --job-name=hidris-sim
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=1
#SBATCH --time=04:00:00
#SBATCH --output=%x-%j.out

# One ANUGA simulation on the HPC. Submitted by the API
# (POST /api/instances/{id}/simulate?target=hpc), which exports:
#   PUBLIC_ID  the instance to solve
#   JOB_ID     the id the frontend is streaming on (sim:events:$JOB_ID)
#
# Progress and completion are published to the cluster's Redis over Tailscale
# using the same events.py contract as the in-cluster worker, so the API's SSE
# relay and the frontend progress bar work identically for both targets.

command -v singularity >/dev/null || module load singularity 2>/dev/null || true

CONTAINERS="$HOME/containers"
SIF="$CONTAINERS/anuga-gpu.sif"
ENVFILE="$CONTAINERS/worker.env"

: "${PUBLIC_ID:?PUBLIC_ID not exported by sbatch}"
: "${JOB_ID:?JOB_ID not exported by sbatch}"

WORKDIR="$CONTAINERS/sim-${SLURM_JOB_ID}"
mkdir -p "$WORKDIR"

# Persistent per-node tailscale identity: nodes are non-ephemeral, so reusing
# the state file keeps one device per compute node instead of one per job.
STATEROOT="$CONTAINERS/ts-state/$(hostname -s)"
mkdir -p "$STATEROOT"

for f in "$SIF" "$ENVFILE"; do
  [[ -r "$f" ]] || { echo "missing or unreadable: $f" >&2; exit 2; }
done

echo "host=$(hostname) job=$SLURM_JOB_ID instance=$PUBLIC_ID stream=$JOB_ID"
nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader 2>/dev/null || true

# Serialise the tailnet phase per node — two tailscaled against one state file
# would fight over the node key.
exec 9>"$STATEROOT/.lock"
flock -w 1800 9 || { echo "timed out waiting for tailnet lock" >&2; exit 3; }

# --nv exposes the host NVIDIA driver. --cleanenv drops the login shell env so
# only --env-file and --env apply; PUBLIC_ID/JOB_ID must therefore be passed
# explicitly rather than inherited.
singularity run \
  --nv \
  --cleanenv \
  --env-file "$ENVFILE" \
  --env "PUBLIC_ID=${PUBLIC_ID},JOB_ID=${JOB_ID},HPC_WORKDIR=/scratch,HPC_STATEDIR=/ts-state,SLURM_JOB_ID=${SLURM_JOB_ID}" \
  -B "$WORKDIR:/scratch" \
  -B "$STATEROOT:/ts-state" \
  "$SIF"
rc=$?

echo "=== exit $rc ==="
# The result is spooled to $WORKDIR/solution-*.bin.gz before the DB write, so a
# failed upload leaves something replayable rather than three hours wasted.
ls -lh "$WORKDIR"/solution-*.bin.gz 2>/dev/null || true
exit $rc
