#!/usr/bin/env bash
# Inside half of the netns transport, generalised: brings up a real tailscale0
# inside an unprivileged user namespace, then execs whatever command it is given.
#
# Same mechanism as services/hpc-probe/netns-run.sh (which is where it was
# proven on vrhpc1); kept as a separate copy because the SIFs are built from
# different directories and this repo already duplicates events.py/db.py the
# same way. Fix bugs in both.
#
# Why netns rather than the SOCKS proxy: inside `unshare -Ur -n` we hold
# CAP_NET_ADMIN over our own network namespace, so tailscaled creates a genuine
# interface and the kernel routes 100.x.y.z normally. libpq/psycopg2 therefore
# works with no proxy, at native throughput — proxychains works too but is a
# userspace TCP/IP path that caps in the hundreds of Mbit.
#
#   $1  fifo the parent writes to once slirp4netns has configured tap0
#   $2  writable workdir
#   $3+ command to exec once the tailnet is up
set -uo pipefail

FIFO="$1"; shift
WORKDIR="$1"; shift

read -r _ < "$FIFO"

ip link set lo up 2>/dev/null || true
if ! ip addr show tap0 >/dev/null 2>&1; then
  echo "netns: tap0 never appeared — slirp4netns did not attach" >&2
  exit 3
fi

printf 'nameserver 100.100.100.100\nnameserver 10.0.2.3\n' > "$WORKDIR/resolv.conf"
mount --bind "$WORKDIR/resolv.conf" /etc/resolv.conf 2>/dev/null \
  || echo "netns: WARN could not bind /etc/resolv.conf" >&2

STATEDIR="${HPC_STATEDIR:-$WORKDIR/ts-state}"
SOCKET="$WORKDIR/tailscaled.sock"
TSLOG="$WORKDIR/tailscaled.log"
mkdir -p "$STATEDIR"

tailscaled --state="$STATEDIR/tailscaled.state" --socket="$SOCKET" \
  --tun=tailscale0 --port=0 >"$TSLOG" 2>&1 &
TS_PID=$!

cleanup() {
  if kill -0 "$TS_PID" 2>/dev/null; then
    # `down`, not `logout`: nodes are non-ephemeral and the state file is the
    # device identity. logout would mint a new device on every simulation.
    tailscale --socket="$SOCKET" down >/dev/null 2>&1 || true
    kill "$TS_PID" 2>/dev/null || true
    wait "$TS_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do [[ -S "$SOCKET" ]] && break; sleep 1; done
[[ -S "$SOCKET" ]] || { echo "netns: tailscaled socket never appeared" >&2; tail -30 "$TSLOG" >&2; exit 4; }

# --reset is load-bearing, not defensive. The state dir is shared with the
# proxychains fallback in hpc-entrypoint.sh, which brings tailscale up with
# --accept-dns=false; this path does not, because it manages resolv.conf
# itself. Without --reset, `tailscale up` sees the stored prefs disagree and
# refuses outright ("changing settings via 'tailscale up' requires mentioning
# all"), so one fallback run poisons every later netns run on that node.
UP_ARGS=(--reset --hostname="${TS_HOSTNAME:-hidris-worker-$(hostname -s)}" --accept-routes)
[[ -n "${TS_TAGS:-}" ]] && UP_ARGS+=(--advertise-tags="$TS_TAGS")
# The auth key must not reach the log: these Slurm .out files land in a shared
# home on the cluster, and `tailscale up` echoes the full command line back on
# failure. Capture and filter rather than letting it through.
if ! up_err=$(tailscale --socket="$SOCKET" up --authkey="${TS_AUTHKEY:?}" "${UP_ARGS[@]}" 2>&1); then
  echo "netns: tailscale up failed" >&2
  printf '%s\n' "$up_err" | sed -e 's/tskey-[A-Za-z0-9-]*/tskey-<redacted>/g' >&2
  sed -e 's/tskey-[A-Za-z0-9-]*/tskey-<redacted>/g' "$TSLOG" | tail -30 >&2
  exit 5
fi

# The search domain is what makes a BARE name like "hidris-db" resolve. Without
# it tailscaled forwards the query upstream and gets SERVFAIL.
SUFFIX="$(tailscale --socket="$SOCKET" status --json 2>/dev/null \
          | python -c 'import json,sys; print(json.load(sys.stdin).get("MagicDNSSuffix",""))' 2>/dev/null)"
[[ -n "$SUFFIX" ]] && printf 'search %s\nnameserver 100.100.100.100\nnameserver 10.0.2.3\n' \
  "$SUFFIX" > "$WORKDIR/resolv.conf"

for _ in $(seq 1 20); do
  getent hosts "${DB_HOST:-hidris-db}" >/dev/null 2>&1 && break
  sleep 1
done
echo "netns: ${DB_HOST:-hidris-db} -> $(getent hosts "${DB_HOST:-hidris-db}" | awk '{print $1}' | tr '\n' ' ')" >&2

"$@"
