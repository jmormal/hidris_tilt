#!/bin/bash
#SBATCH --job-name=hidris-probe
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH --output=%x-%j.out

# Connectivity probe — no GPU, no --gres, short walltime, so it queues fast.
# The point is to answer the network question on a COMPUTE node specifically:
# the login node may well have connectivity the compute nodes lack.
#
#   sbatch ~/containers/run-probe.slurm
#   cat hidris-probe-<jobid>.out            # human summary
#   cat ~/containers/probe-<jobid>/probe-result.json   # machine-readable
#
# Cluster runs Singularity 3.7.3; some sites need `module load singularity`.
command -v singularity >/dev/null || module load singularity 2>/dev/null || true

CONTAINERS="$HOME/containers"
SIF="$CONTAINERS/hidris-probe.sif"
ENVFILE="$CONTAINERS/probe.env"

# Per-job workdir, kept afterwards: this is where the JSON the API reads back
# lands, and keeping it per-job means concurrent probes cannot clobber each
# other's tailscaled state directory.
WORKDIR="$CONTAINERS/probe-${SLURM_JOB_ID}"
mkdir -p "$WORKDIR"

# Persistent tailscale identity, per compute node. Nodes are non-ephemeral, so
# the state file is the device: keeping it means vrhpcN re-registers as the same
# device every run rather than leaving one dead entry per job behind.
STATEROOT="$CONTAINERS/ts-state/$(hostname -s)"
mkdir -p "$STATEROOT"

for f in "$SIF" "$ENVFILE"; do
  [[ -r "$f" ]] || { echo "missing or unreadable: $f" >&2; exit 2; }
done

# Two jobs on one node would run two tailscaled against the same state file and
# fight over the node key. Serialize on it; the tailnet phase is short, so a
# waiting job gets through quickly.
exec 9>"$STATEROOT/.lock"
if ! flock -w 900 9; then
  echo "timed out waiting for the tailnet lock on $(hostname -s)" >&2
  exit 3
fi

echo "host=$(hostname) job=$SLURM_JOB_ID workdir=$WORKDIR stateroot=$STATEROOT"

# --cleanenv  drop the login shell's environment so only --env-file applies
# -B          the SIF root is read-only squashfs; all writes go here
# No --nv: this probe never touches a GPU.
singularity run \
  --cleanenv \
  --env-file "$ENVFILE" \
  --env "PROBE_WORKDIR=/scratch,PROBE_STATEDIR=/ts-state,SLURM_JOB_ID=${SLURM_JOB_ID}" \
  -B "$WORKDIR:/scratch" \
  -B "$STATEROOT:/ts-state" \
  "$SIF"
rc=$?

echo
echo "=== probe-result.json ==="
cat "$WORKDIR/probe-result.json" 2>/dev/null || echo "(no result file produced)"
echo "=== exit $rc ==="
exit $rc
