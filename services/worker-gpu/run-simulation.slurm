#!/bin/bash
#SBATCH --job-name=hidris-sim
#SBATCH --gres=gpu:1
# One MPI rank per GPU. The API overrides --gres from the UI; HPC_NTASKS has to
# follow it, which is what the SLURM_GPUS_ON_NODE fallback below is for.
#SBATCH --cpus-per-task=4
# Memory is sized for the RESULT, not the solve. The solve itself is modest
# (~10GB for a 1.8M-triangle domain), but _snapshot keeps three float32 arrays
# per output frame for the whole run, and _finalize_result then np.stack()s
# copies of all three and derives four more full-size (T,N) arrays before the
# wet filter drops anything. Peak is roughly 10 x T x N x 4 bytes: for
# T=1267 frames x N=1.8M triangles that is ~90GB, against a solve that never
# exceeds 11GB. The nodes have 490GB, so buy headroom rather than lose a
# finished 8-minute solve to an OOM kill in the last step.
#SBATCH --mem=200G
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
# The DB password does NOT travel in worker.env. Singularity 3.7.3 pipes every
# --env / --env-file value through a shell before injecting it, so a password
# containing $ loses everything from the $ on, and one containing a " aborts the
# container with "reached EOF without closing quote". That is true of --env,
# --env-file and SINGULARITYENV_ alike — there is no literal-passing mode.
# libpq reads a pgpass file itself, with no shell in the path, so the secret
# goes in here instead and db.py connects with password=None.
PGPASS="$CONTAINERS/pgpass"
# The SIF is 16GB and takes ~40 min to rebuild plus ~7 min to re-upload, so a
# one-line fix to either transport script is bind-mounted over the baked-in
# copy rather than rebuilt in. Both files still live in the image; these just
# win when present. Drop the overrides once the SIF is next rebuilt.
ENTRYPOINT_OVERRIDE="$CONTAINERS/hpc-entrypoint.sh"
NETNS_OVERRIDE="$CONTAINERS/netns-run-worker.sh"
[[ -r "$ENTRYPOINT_OVERRIDE" ]] || ENTRYPOINT_OVERRIDE=""
[[ -r "$NETNS_OVERRIDE" ]] || NETNS_OVERRIDE=""

# ANUGA compiled for THIS cluster's CPU. The SIF was built on a Zen 5 laptop,
# where nvc's default -tp=host emitted AVX-512; the EPYC 7453 nodes here are
# Zen 3 and have none, so the baked-in build dies on `import anuga` with SIGILL
# (exit 132, "Illegal instruction", and no Python traceback — numpy and scipy
# import fine, being generic wheels, which makes it look like an ANUGA bug).
# build-anuga.slurm recompiles just this 24MB package on a compute node, where
# nvc's host default IS the target. Bound over the image's copy.
# Remove once DockerfileSingularity's CPU_ARCH=zen3 build has been baked into
# a fresh SIF.
ANUGA_OVERRIDE=$(ls -d "$CONTAINERS"/anuga-zen3/extracted/anuga 2>/dev/null | head -1)
# Worker sources, same reason as the transport scripts: a change to tasks.py or
# hpc_run.py should not cost a 16GB rebuild and re-upload. Bound per FILE, not
# as a directory — /app/src also holds the 13GB DEM, and binding the directory
# would hide it.
SRC_OVERRIDE="$CONTAINERS/src-override"
[[ -d "$SRC_OVERRIDE" ]] || SRC_OVERRIDE=""
if [[ -z "$ANUGA_OVERRIDE" ]]; then
  # Say so rather than falling back silently: without the override the image's
  # own AVX-512 build runs and dies with SIGILL, which looks like a fresh
  # problem instead of a missing bind. Not fatal — once the SIF is rebuilt with
  # CPU_ARCH=zen3 the override is expected to be absent.
  echo "WARN: no anuga override at $CONTAINERS/anuga-zen3/extracted/anuga;" \
       "using the image's build (expect SIGILL unless the SIF was rebuilt for zen3)" >&2
fi

: "${PUBLIC_ID:?PUBLIC_ID not exported by sbatch}"
: "${JOB_ID:?JOB_ID not exported by sbatch}"

WORKDIR="$CONTAINERS/sim-${SLURM_JOB_ID}"
# tmp/ and nv-cache/ must exist before the container starts: worker.env points
# TMPDIR and CUDA_CACHE_PATH at them, and neither Python's tempfile nor the
# CUDA JIT cache creates a missing directory — they just fail.
mkdir -p "$WORKDIR" "$WORKDIR/tmp" "$WORKDIR/nv-cache"

# Persistent per-node tailscale identity: nodes are non-ephemeral, so reusing
# the state file keeps one device per compute node instead of one per job.
STATEROOT="$CONTAINERS/ts-state/$(hostname -s)"
mkdir -p "$STATEROOT"

for f in "$SIF" "$ENVFILE" "$PGPASS"; do
  [[ -r "$f" ]] || { echo "missing or unreadable: $f" >&2; exit 2; }
done

# Slurm creates the .out file before this script runs, so a umask here is too
# late — chmod it instead. $HOME is a shared cluster filesystem and tailscale
# echoes its full command line (auth key included) when `up` fails, so these
# logs must not be world-readable.
chmod 600 "$CONTAINERS/hidris-sim-${SLURM_JOB_ID}.out" 2>/dev/null || true

# One rank per allocated GPU. SLURM_GPUS_ON_NODE reflects whatever --gres the
# submitter actually asked for, so this follows the UI's GPU selector without
# the API having to pass a second, redundant number that could disagree with it.
NTASKS="${HPC_NTASKS:-${SLURM_GPUS_ON_NODE:-1}}"
# Storm cubes are ~666MB and immutable for a given storm_ref, but live in
# Postgres across the tailnet — a DERP-relayed link measured at ~3 MB/s, so
# ~4 minutes per fetch. Cache them in the shared containers dir so repeat runs
# read from disk instead. Cleared by hand; they are pure cache.
mkdir -p "$CONTAINERS/storm-cache"
echo "ranks=$NTASKS (gpus on node=${SLURM_GPUS_ON_NODE:-unset})"

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
  --env "PUBLIC_ID=${PUBLIC_ID},JOB_ID=${JOB_ID},HPC_WORKDIR=/scratch,HPC_STATEDIR=/ts-state,SLURM_JOB_ID=${SLURM_JOB_ID},PGPASSFILE=/pgpass,HPC_NTASKS=${NTASKS},HPC_STORM_CACHE=/storm-cache" \
  -B "$CONTAINERS/storm-cache:/storm-cache" \
  -B "$WORKDIR:/scratch" \
  -B "$STATEROOT:/ts-state" \
  -B "$PGPASS:/pgpass:ro" \
  ${ENTRYPOINT_OVERRIDE:+-B "$ENTRYPOINT_OVERRIDE:/opt/hpc/entrypoint.sh:ro"} \
  ${NETNS_OVERRIDE:+-B "$NETNS_OVERRIDE:/opt/hpc/netns-run.sh:ro"} \
  ${ANUGA_OVERRIDE:+-B "$ANUGA_OVERRIDE:/usr/local/lib/python3.10/dist-packages/anuga:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/tasks.py:/app/src/tasks.py:ro"} \
  ${SRC_OVERRIDE:+-B "$SRC_OVERRIDE/hpc_run.py:/app/src/hpc_run.py:ro"} \
  "$SIF"
rc=$?

echo "=== exit $rc ==="
# The result is spooled to $WORKDIR/solution-*.bin.gz before the DB write, so a
# failed upload leaves something replayable rather than three hours wasted.
ls -lh "$WORKDIR"/solution-*.bin.gz 2>/dev/null || true
exit $rc
