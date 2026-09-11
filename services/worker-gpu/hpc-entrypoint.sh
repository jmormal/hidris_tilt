#!/usr/bin/env bash
# SIF entrypoint for an HPC simulation. Slurm is the scheduler here, so there is
# no RQ worker loop and no queue — this runs exactly one simulation and exits.
#
# Brings the tailnet up first (the DB and Redis are only reachable over it),
# then runs src/hpc_run.py inside that namespace.
#
# Required env (from --env-file worker.env plus --env at submit time):
#   PUBLIC_ID    instance to solve
#   JOB_ID       id the API told the frontend to stream on (sim:events:$JOB_ID)
#   TS_AUTHKEY   tailscale auth key, tagged tag:hpc
#   DB_HOST/DB_NAME/PG_USER/PG_PASSWORD, REDIS_URL
set -uo pipefail

WORKDIR="${HPC_WORKDIR:-/scratch}"
mkdir -p "$WORKDIR"

: "${PUBLIC_ID:?PUBLIC_ID is unset}"
: "${JOB_ID:?JOB_ID is unset}"

# How the solver is launched. HPC_NTASKS>1 puts one MPI rank on each GPU —
# ANUGA assigns devices round-robin by rank (gpu_domain_init: rank % ndevices),
# so ranks are the only way to use more than one card.
#
# mpirun INSIDE the container, not srun outside it: Slurm's PMI does not reach
# the container's OpenMPI, and two srun tasks each come up as an independent
# 1-rank world (numprocs=1 twice, one GPU idle). --bind-to none because
# OpenMPI's default binding collides with the cpuset Slurm hands us and dies
# with hwloc_set_cpubind "Error".
solver_cmd() {
  local n="${HPC_NTASKS:-1}"
  if [[ "$n" -gt 1 ]]; then
    # NOT --oversubscribe. Slurm gives us --cpus-per-task >= ranks, so the node
    # is not oversubscribed, and that flag makes OpenMPI switch to
    # yield-when-idle polling: every MPI wait becomes a sched_yield loop instead
    # of a busy-wait. Measured cost with 2 ranks: ~43ms per timestep, which
    # turned a 43-second solve into 41 minutes — same dt, same step count, all
    # of it latency. mpi_yield_when_idle=0 pins the fast path explicitly.
    #
    # --bind-to none stays: OpenMPI's default binding collides with the cpuset
    # Slurm hands us and aborts with hwloc_set_cpubind "Error".
    echo mpirun --bind-to none --mca mpi_yield_when_idle 0 -n "$n" python /app/src/hpc_run.py
  else
    echo python /app/src/hpc_run.py
  fi
}

run_direct() {
  echo "==> no tailnet requested (HPC_NO_TAILNET=1); running against \$DB_HOST as-is" >&2
  $(solver_cmd)
}

run_netns() {
  local fifo="$WORKDIR/netns.fifo"
  rm -f "$fifo"; mkfifo "$fifo" || return 1

  unshare --user --map-root-user --net --mount \
    /opt/hpc/netns-run.sh "$fifo" "$WORKDIR" $(solver_cmd) &
  local nspid=$!

  slirp4netns --configure --mtu=65520 --disable-host-loopback \
    "$nspid" tap0 >"$WORKDIR/slirp4netns.log" 2>&1 &
  local slirp=$!

  sleep 2
  if ! kill -0 "$slirp" 2>/dev/null; then
    echo "!! slirp4netns died on startup" >&2
    tail -20 "$WORKDIR/slirp4netns.log" >&2
    echo go > "$fifo" 2>/dev/null || true
    wait "$nspid" 2>/dev/null
    return 1
  fi
  echo go > "$fifo"

  wait "$nspid"; local rc=$?
  kill "$slirp" 2>/dev/null || true; wait "$slirp" 2>/dev/null || true
  rm -f "$fifo"
  return $rc
}

if [[ "${HPC_NO_TAILNET:-0}" == "1" ]]; then
  run_direct
  exit $?
fi

# netns first: a real TUN means psycopg2 works unmodified at native throughput.
# Measured working on vrhpc1 (see services/hpc-probe). proxychains is the
# fallback — it also carries libpq, but through a userspace TCP/IP stack that
# caps throughput in the hundreds of Mbit.
if run_netns; then
  exit 0
fi

echo "!! netns transport failed; falling back to userspace + proxychains" >&2

STATEDIR="${HPC_STATEDIR:-$WORKDIR/ts-state}"
mkdir -p "$STATEDIR"
SOCKET="$WORKDIR/tailscaled-fallback.sock"
tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 \
  --statedir="$STATEDIR" --socket="$SOCKET" >"$WORKDIR/tailscaled-fb.log" 2>&1 &
TS_PID=$!
trap 'tailscale --socket="$SOCKET" down >/dev/null 2>&1 || true; kill "$TS_PID" 2>/dev/null' EXIT

for _ in $(seq 1 30); do [[ -S "$SOCKET" ]] && break; sleep 1; done
# --reset for the same reason as netns-run.sh: the two transports share a state
# dir but disagree about --accept-dns, and without it whichever ran first makes
# the other refuse to start.
UP_ARGS=(--reset --hostname="${TS_HOSTNAME:-hidris-worker-$(hostname -s)}" --accept-routes --accept-dns=false)
[[ -n "${TS_TAGS:-}" ]] && UP_ARGS+=(--advertise-tags="$TS_TAGS")
if ! up_err=$(tailscale --socket="$SOCKET" up --authkey="${TS_AUTHKEY:?}" "${UP_ARGS[@]}" 2>&1); then
  # Never let the key into a Slurm .out file — see netns-run.sh.
  printf '%s\n' "$up_err" | sed -e 's/tskey-[A-Za-z0-9-]*/tskey-<redacted>/g' >&2
  exit 5
fi

exec proxychains4 -f /etc/proxychains4.conf -q $(solver_cmd)
