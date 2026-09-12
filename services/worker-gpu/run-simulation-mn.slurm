#!/bin/bash
#SBATCH --job-name=hidris-mn
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=2
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=2
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=%x-%j.out
#
# Multi-node ANUGA. One MPI rank per GPU, ranks spread across nodes, launched by
# srun rather than mpirun so Slurm wires the communicator across machines.
#
# Two settings here are not optional, and each cost a wrong conclusion before
# being understood:
#
#   NO --cleanenv. srun exports ~13 PMIX_* variables and they are how the
#   container's OpenMPI finds the PMIx server. Strip them and each task comes up
#   as its own 1-rank world: numprocs=1 on every task, no error, GPUs idle.
#
#   btl/oob_tcp_if_include. Every node carries docker0 on 172.17.0.1/16 — the
#   SAME address on all of them — so OpenMPI concludes they share a subnet and
#   blocks forever trying to reach itself. Pinning to a real interface takes a
#   64MB broadcast from "hangs past a 5 minute limit" to 0.07s.
set -uo pipefail

CONTAINERS="$HOME/containers"
SIF="$CONTAINERS/anuga-gpu.sif"
ENVFILE="$CONTAINERS/worker.env"
PGPASS="$CONTAINERS/pgpass"

: "${PUBLIC_ID:?PUBLIC_ID not exported by sbatch}"
: "${JOB_ID:?JOB_ID not exported by sbatch}"

WORKDIR="$CONTAINERS/sim-${SLURM_JOB_ID}"
mkdir -p "$WORKDIR" "$WORKDIR/tmp" "$WORKDIR/nv-cache" "$CONTAINERS/storm-cache" "$CONTAINERS/ts-state"
chmod 600 "$CONTAINERS/hidris-mn-${SLURM_JOB_ID}.out" 2>/dev/null || true

for f in "$SIF" "$ENVFILE" "$PGPASS"; do
  [[ -r "$f" ]] || { echo "missing or unreadable: $f" >&2; exit 2; }
done

# Overrides, same rationale as the single-node script: a one-line fix should not
# cost a 16GB rebuild and upload.
ANUGA_OVERRIDE=$(ls -d "$CONTAINERS"/anuga-zen3/extracted/anuga 2>/dev/null | head -1)
SRC_OVERRIDE="$CONTAINERS/src-override"
[[ -d "$SRC_OVERRIDE" ]] || SRC_OVERRIDE=""
MN_ENTRY="$CONTAINERS/mn-entrypoint.sh"

NET_IF="${HPC_MPI_IF:-eno2}"
echo "nodes=$SLURM_JOB_NUM_NODES tasks=$SLURM_NTASKS gpus/node=${SLURM_GPUS_ON_NODE:-?} iface=$NET_IF"
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader 2>/dev/null | head -1 || true

# --nv exposes the driver. Deliberately NOT --cleanenv (see above); the env-file
# still applies on top of the inherited environment.
srun --mpi=pmix singularity exec \
  --nv \
  --env-file "$ENVFILE" \
  --env "PUBLIC_ID=${PUBLIC_ID},JOB_ID=${JOB_ID},HPC_WORKDIR=/scratch,HPC_STATE_ROOT=/ts-state-root,SLURM_JOB_ID=${SLURM_JOB_ID},PGPASSFILE=/pgpass,HPC_STORM_CACHE=/storm-cache,OMPI_MCA_btl_tcp_if_include=${NET_IF},OMPI_MCA_oob_tcp_if_include=${NET_IF}" \
  -B "$WORKDIR:/scratch" \
  -B "$PGPASS:/pgpass:ro" \
  -B "$CONTAINERS/storm-cache:/storm-cache" \
  -B "$CONTAINERS/ts-state:/ts-state-root" \
  -B "$MN_ENTRY:/opt/hpc/mn-entrypoint.sh:ro" \
  -B /var/run -B /tmp \
  ${ANUGA_OVERRIDE:+-B "$ANUGA_OVERRIDE:/usr/local/lib/python3.10/dist-packages/anuga:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/tasks.py:/app/src/tasks.py:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/hpc_run.py:/app/src/hpc_run.py:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/db.py:/app/src/db.py:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/db_proxy.py:/app/src/db_proxy.py:ro"} \
  "$SIF" /opt/hpc/mn-entrypoint.sh
rc=$?

echo "=== exit $rc ==="
ls -lh "$WORKDIR"/solution-*.bin.gz 2>/dev/null || true
exit $rc
