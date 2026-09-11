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

run_direct() {
  echo "==> no tailnet requested (HPC_NO_TAILNET=1); running against \$DB_HOST as-is" >&2
  python /app/src/hpc_run.py
}

run_netns() {
  local fifo="$WORKDIR/netns.fifo"
  rm -f "$fifo"; mkfifo "$fifo" || return 1

  unshare --user --map-root-user --net --mount \
    /opt/hpc/netns-run.sh "$fifo" "$WORKDIR" python /app/src/hpc_run.py &
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

exec proxychains4 -f /etc/proxychains4.conf -q python /app/src/hpc_run.py
