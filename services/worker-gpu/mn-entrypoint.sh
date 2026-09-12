#!/usr/bin/env bash
# Multi-node SIF entrypoint. One container per Slurm task; srun --mpi=pmix wires
# them into a single MPI communicator across nodes.
#
# The single-node entrypoint puts every rank inside `unshare --net` so the
# tailnet works. That cannot be used here: a network namespace isolates MPI too,
# so ranks inside it cannot reach other nodes over eno2. The namespace and
# multi-node MPI are mutually exclusive.
#
# The way out is that only RANK 0 needs the tailnet. It alone reads the
# instance, fetches the storm cube, writes the solution and publishes progress;
# every other rank does arithmetic and talks MPI. So:
#
#   rank 0   tailscaled in userspace mode with a SOCKS5 port, and python run
#            under proxychains4 whose localnet rules send the cluster subnets
#            direct and only the tailnet (100.64/10) through the proxy.
#   rank !0  python, unmodified. No tailscale, no proxychains, no namespace.
#
# SLURM_PROCID is set by srun before MPI starts, so the branch is available
# before anuga's myid exists.
set -uo pipefail

WORKDIR="${HPC_WORKDIR:-/scratch}"
RANK="${SLURM_PROCID:-0}"
mkdir -p "$WORKDIR"

: "${PUBLIC_ID:?PUBLIC_ID is unset}"
: "${JOB_ID:?JOB_ID is unset}"

if [[ "$RANK" != "0" ]]; then
  echo "==> rank $RANK: solver only, no tailnet" >&2
  exec python /app/src/hpc_run.py
fi

# ---- rank 0 only, from here ------------------------------------------------
# Per NODE, not per job: the state file is the tailnet device identity, so a
# fresh directory each run would mint a new device every time and the tailnet
# would fill with dead tag:hpc entries — which is exactly the mess
# tailnet-cleanup.sh exists to undo. Only rank 0 runs tailscaled, so two
# concurrent multi-node jobs would only collide if both placed rank 0 on the
# same node; rare enough to accept for now.
STATE_ROOT="${HPC_STATE_ROOT:-$WORKDIR}"
STATEDIR="$STATE_ROOT/$(hostname -s)"
mkdir -p "$STATEDIR" 2>/dev/null || STATEDIR="$WORKDIR/ts-state"
mkdir -p "$STATEDIR"
SOCKET="$WORKDIR/tailscaled.sock"

tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 \
  --statedir="$STATEDIR" --socket="$SOCKET" >"$WORKDIR/tailscaled.log" 2>&1 &
TS_PID=$!
trap 'tailscale --socket="$SOCKET" down >/dev/null 2>&1 || true; kill "$TS_PID" 2>/dev/null' EXIT

for _ in $(seq 1 30); do [[ -S "$SOCKET" ]] && break; sleep 1; done
[[ -S "$SOCKET" ]] || { echo "rank 0: tailscaled socket never appeared" >&2; exit 4; }

# --reset for the same reason as the single-node path: the stored prefs may come
# from a previous run with different flags, and `tailscale up` refuses rather
# than reconciling.
UP_ARGS=(--reset --hostname="${TS_HOSTNAME:-hidris-mn-$(hostname -s)}"
         --accept-routes --accept-dns=false)
[[ -n "${TS_TAGS:-}" ]] && UP_ARGS+=(--advertise-tags="$TS_TAGS")
if ! up_err=$(tailscale --socket="$SOCKET" up --authkey="${TS_AUTHKEY:?}" "${UP_ARGS[@]}" 2>&1); then
  # Never let the key reach a shared-filesystem log.
  printf '%s\n' "$up_err" | sed -e 's/tskey-[A-Za-z0-9-]*/tskey-<redacted>/g' >&2
  exit 5
fi

# localnet entries are evaluated in order and mean "connect directly". The
# cluster fabrics must bypass the proxy or MPI would be tunnelled through a
# userspace SOCKS stack — which is both catastrophic for latency and pointless,
# since the peers are not on the tailnet.
cat > "$WORKDIR/proxychains-mn.conf" <<'PC'
strict_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 10000
localnet 127.0.0.0/255.0.0.0
localnet 192.168.0.0/255.255.0.0
localnet 172.16.0.0/255.240.0.0
localnet 10.0.0.0/255.0.0.0
[ProxyList]
socks5 127.0.0.1 1055
PC

# The solver itself is NOT proxied. proxychains works by LD_PRELOADing
# connect(), and inside an MPI process that breaks the library outright —
# measured: MPI_ERR_INTERN inside PyMPI_bcast, with the localnet bypasses in
# place. Instead hpc_run.py shells out to db_proxy.py under this config for the
# handful of operations that need the tailnet (read the setup, fetch the storm
# cube, store the solution, publish an event). Those subprocesses are proxied;
# the long-lived MPI process never is.
export HPC_DB_PROXY_CONF="$WORKDIR/proxychains-mn.conf"

echo "==> rank 0: tailnet up, DB/Redis via proxied subprocesses, MPI direct" >&2
exec python /app/src/hpc_run.py
