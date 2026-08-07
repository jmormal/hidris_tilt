#!/usr/bin/env bash
# Brings up tailscaled in userspace mode, then runs probe.py once per transport
# and merges the results into one JSON document.
#
# Runs entirely unprivileged: userspace-networking means tailscaled never asks
# the kernel for an interface, so no CAP_NET_ADMIN and no admin involvement.
#
# Requires (from --env-file):
#   TS_AUTHKEY    auth key, or an OAuth client secret with ?ephemeral=true
#   PG_PASSWORD   the CNPG app password
# Writes its state and logs under $PROBE_WORKDIR, which MUST be a bind-mounted
# writable path — the SIF root is read-only squashfs.
set -uo pipefail

WORKDIR="${PROBE_WORKDIR:-/scratch}"
# Persistent, per-node, and deliberately NOT under the per-job workdir: with
# non-ephemeral nodes the state file IS the device identity, so reusing it
# re-registers as the same device every run instead of leaving one stale entry
# per Slurm job. Falls back to the workdir if the runner did not bind it.
STATEROOT="${PROBE_STATEDIR:-/ts-state}"
[[ -d "$STATEROOT" ]] || STATEROOT="$WORKDIR/ts-state"
STATEDIR="$STATEROOT/userspace"
SOCKET="$WORKDIR/tailscaled.sock"
TSLOG="$WORKDIR/tailscaled.log"
mkdir -p "$STATEDIR"

export TS_SOCKET="$SOCKET"
export TS_SOCKS_HOST="${TS_SOCKS_HOST:-127.0.0.1}"
export TS_SOCKS_PORT="${TS_SOCKS_PORT:-1055}"

# Keyed to the node, not the job: one stable device per compute node, reused by
# every job that lands there. Including SLURM_JOB_ID here would mint a new
# device per run, which is exactly what non-ephemeral nodes make expensive.
HOSTNAME_TS="${TS_HOSTNAME:-hidris-probe-$(hostname -s)}"

cleanup() {
  # `down`, NOT `logout`: logout discards the node key, so the next run would
  # register as a brand-new device and the device list would grow one entry per
  # job. `down` just disconnects and leaves the identity in the state file.
  if [[ -n "${TS_PID:-}" ]] && kill -0 "$TS_PID" 2>/dev/null; then
    tailscale --socket "$SOCKET" down >/dev/null 2>&1 || true
    kill "$TS_PID" 2>/dev/null || true
    wait "$TS_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "==> starting tailscaled (userspace-networking, SOCKS5 on :$TS_SOCKS_PORT)" >&2
tailscaled \
  --tun=userspace-networking \
  --socks5-server="${TS_SOCKS_HOST}:${TS_SOCKS_PORT}" \
  --outbound-http-proxy-listen="${TS_SOCKS_HOST}:$((TS_SOCKS_PORT + 1))" \
  --statedir="$STATEDIR" \
  --socket="$SOCKET" \
  >"$TSLOG" 2>&1 &
TS_PID=$!

# Wait for the local API socket before talking to it; `tailscale up` against a
# daemon that has not finished starting fails in a confusing way.
for _ in $(seq 1 30); do
  [[ -S "$SOCKET" ]] && break
  sleep 1
done
if [[ ! -S "$SOCKET" ]]; then
  echo "!! tailscaled never created $SOCKET; log follows" >&2
  cat "$TSLOG" >&2
  exit 2
fi

echo "==> tailscale up as $HOSTNAME_TS" >&2
UP_ARGS=(--hostname="$HOSTNAME_TS" --accept-routes --accept-dns=false)
# --accept-dns=false is not a preference: without root tailscaled cannot rewrite
# /etc/resolv.conf anyway, and asking it to try produces a misleading health
# warning. MagicDNS still resolves through the SOCKS proxy, where tailscaled
# does the lookup itself.
[[ -n "${TS_TAGS:-}" ]] && UP_ARGS+=(--advertise-tags="$TS_TAGS")

if ! tailscale --socket "$SOCKET" up --authkey="${TS_AUTHKEY:?TS_AUTHKEY is unset — see probe.env}" \
      "${UP_ARGS[@]}" 2>&1 | sed 's/^/    /' >&2; then
  echo "!! tailscale up failed; daemon log follows" >&2
  tail -40 "$TSLOG" >&2
  exit 2
fi

# Peer state and DERP negotiation settle a moment after `up` returns.
sleep 3
tailscale --socket "$SOCKET" status 2>&1 | sed 's/^/    /' >&2 || true

run_mode() {
  local mode="$1"; shift
  local out="$WORKDIR/probe-${mode}.json"
  echo >&2
  echo "############ transport: $mode ############" >&2
  PROBE_MODE="$mode" "$@" python /opt/probe/probe.py >"$out"
  echo "$out"
}

# direct       — expected to fail without a TUN; run first because it also
#                collects the node capability and tailscale status sections.
# socks        — pure-Python clients only (redis-py, pg8000).
# proxychains  — catches libpq too, so this is the one that decides whether the
#                existing worker db.py can run unmodified.
run_mode direct                                          >/dev/null || true
run_mode socks                                           >/dev/null || true
run_mode proxychains proxychains4 -f /etc/proxychains4.conf -q >/dev/null || true

# netns — the best outcome if it works: a real TUN inside a user namespace, so
# psycopg2 needs no proxy at all. Runs its own tailscaled (see netns-run.sh);
# failure here must not disturb the three transports already measured above.
run_netns() {
  echo >&2
  echo "############ transport: netns (real TUN via unshare + slirp4netns) ############" >&2

  if ! command -v slirp4netns >/dev/null; then
    echo "netns: slirp4netns not present; skipping" >&2
    return 1
  fi
  if ! unshare -Ur -n true 2>/dev/null; then
    echo "netns: user namespaces unavailable on this node; skipping" >&2
    return 1
  fi

  local fifo="$WORKDIR/netns.fifo"
  rm -f "$fifo"
  mkfifo "$fifo" || return 1

  # The child blocks on the fifo until slirp4netns has configured tap0. We need
  # its PID from OUT here, because slirp4netns joins the namespace by pid.
  unshare --user --map-root-user --net --mount \
    /opt/probe/netns-run.sh "$fifo" "$WORKDIR" &
  local nspid=$!

  # --configure sets up tap0 with an address, route and MTU inside the child.
  # --disable-host-loopback stops the namespace reaching the node's own
  # 127.0.0.1, which we neither need nor want to expose.
  slirp4netns --configure --mtu=65520 --disable-host-loopback \
    "$nspid" tap0 >"$WORKDIR/slirp4netns.log" 2>&1 &
  local slirp=$!

  # Give slirp4netns a moment to finish configuring before releasing the child.
  sleep 2
  if ! kill -0 "$slirp" 2>/dev/null; then
    echo "netns: slirp4netns died on startup; log follows" >&2
    tail -20 "$WORKDIR/slirp4netns.log" >&2
    echo go > "$fifo" 2>/dev/null || true   # unblock the child so it can exit
    wait "$nspid" 2>/dev/null
    return 1
  fi
  echo go > "$fifo"

  wait "$nspid"
  local rc=$?
  kill "$slirp" 2>/dev/null || true
  wait "$slirp" 2>/dev/null || true
  rm -f "$fifo"
  return $rc
}

run_netns || true

# One document for the API to read: {"transports": {direct: {...}, ...}}.
FINAL="$WORKDIR/probe-result.json"
python - "$WORKDIR" >"$FINAL" <<'PY'
import json, pathlib, sys

workdir = pathlib.Path(sys.argv[1])
merged = {"schema": 1, "transports": {}}
for mode in ("direct", "socks", "proxychains", "netns"):
    path = workdir / f"probe-{mode}.json"
    try:
        merged["transports"][mode] = json.loads(path.read_text())
    except Exception as exc:
        merged["transports"][mode] = {"error": f"{type(exc).__name__}: {exc}"}

def usable(mode):
    return merged["transports"].get(mode, {}).get("summary", {})

# The headline: which transports work, and can the real worker use one as-is.
merged["verdict"] = {
    "reachable_via": [m for m in merged["transports"] if usable(m).get("usable")],
    "worker_ready_via": [m for m in merged["transports"] if usable(m).get("worker_ready")],
}
json.dump(merged, sys.stdout, indent=2, default=str)
PY

echo >&2
echo "==> merged report: $FINAL" >&2
python -c "
import json,sys
v=json.load(open('$FINAL'))['verdict']
print('    reachable via     :', ', '.join(v['reachable_via']) or 'NOTHING', file=sys.stderr)
print('    worker_ready via  :', ', '.join(v['worker_ready_via']) or 'NOTHING', file=sys.stderr)
"

# Non-zero if no transport reached both services, so sacct shows a FAILED job.
python -c "
import json,sys
sys.exit(0 if json.load(open('$FINAL'))['verdict']['reachable_via'] else 1)
"
