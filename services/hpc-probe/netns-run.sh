#!/usr/bin/env bash
# The inside half of the "netns" transport. Runs under:
#
#   unshare --user --map-root-user --net --mount
#
# In there we are UID 0 with a full capability set over our OWN network
# namespace (CapEff=3fffffffff), so TUNSETIFF succeeds and tailscaled can bring
# up a genuine tailscale0 interface with genuine kernel routes. That is the
# difference that matters: every client on the node routes to 100.x.y.z
# normally, including libpq, so psycopg2 works with no proxy and no code change.
#
# The namespace starts with only loopback and no egress. slirp4netns — running
# OUTSIDE, as the ordinary unprivileged user — attaches a tap0 and translates
# its packets into ordinary socket() calls. Every syscall it makes is one the
# user was already allowed to make; that is why this needs no admin.
#
#   $1  fifo the parent writes to once slirp4netns has configured tap0
#   $2  writable workdir (bind-mounted; the SIF root is read-only squashfs)
set -uo pipefail

FIFO="$1"
WORKDIR="$2"

# Block until slirp4netns has created and configured tap0. Racing it means
# tailscaled starts with no route out and fails in a way that looks like a
# tailnet problem rather than a startup-ordering one.
read -r _ < "$FIFO"

ip link set lo up 2>/dev/null || true

if ! ip addr show tap0 >/dev/null 2>&1; then
  echo "netns: tap0 never appeared — slirp4netns did not attach" >&2
  exit 3
fi
echo "netns: tap0 up: $(ip -4 -o addr show tap0 2>/dev/null | tr -s ' ')" >&2

# MagicDNS needs /etc/resolv.conf to point at tailscaled's resolver, but the SIF
# root is read-only squashfs so tailscaled cannot rewrite it. Bind a writable
# copy over it — we hold CAP_SYS_ADMIN in this namespace, and a bind mount does
# not touch the underlying squashfs.
# 10.0.2.3 is slirp4netns's built-in resolver, kept as the fallback so that
# non-tailnet name resolution still works if MagicDNS is unavailable.
# The `search` line is filled in after login, once we know the tailnet suffix.
printf 'nameserver 100.100.100.100\nnameserver 10.0.2.3\n' > "$WORKDIR/resolv.conf"
if mount --bind "$WORKDIR/resolv.conf" /etc/resolv.conf 2>/dev/null; then
  echo "netns: bound writable /etc/resolv.conf" >&2
else
  echo "netns: WARN could not bind /etc/resolv.conf; MagicDNS may not resolve" >&2
fi

# Persistent per-node state, same reasoning as entrypoint.sh: with
# non-ephemeral nodes the state file is the device identity. Separate subdir
# from the userspace daemon — they are two distinct tailnet nodes and must not
# share a node key.
STATEROOT="${PROBE_STATEDIR:-/ts-state}"
[[ -d "$STATEROOT" ]] || STATEROOT="$WORKDIR/ts-state"
STATEDIR="$STATEROOT/netns"
SOCKET="$WORKDIR/tailscaled-netns.sock"
TSLOG="$WORKDIR/tailscaled-netns.log"
mkdir -p "$STATEDIR"

# Separate statedir and hostname from the userspace-mode daemon in the parent:
# two tailscaled instances sharing a state directory would fight over one node
# key. They cannot collide on ports, being in different network namespaces.
echo "netns: starting tailscaled with a real TUN" >&2
tailscaled \
  --state="$STATEDIR/tailscaled.state" \
  --socket="$SOCKET" \
  --tun=tailscale0 \
  --port=0 \
  >"$TSLOG" 2>&1 &
TS_PID=$!

cleanup() {
  # `down`, not `logout` — see entrypoint.sh. logout would discard the node key
  # and mint a fresh device on every run.
  if kill -0 "$TS_PID" 2>/dev/null; then
    tailscale --socket="$SOCKET" down >/dev/null 2>&1 || true
    kill "$TS_PID" 2>/dev/null || true
    wait "$TS_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do
  [[ -S "$SOCKET" ]] && break
  sleep 1
done
if [[ ! -S "$SOCKET" ]]; then
  echo "netns: tailscaled never created its socket; log follows" >&2
  tail -30 "$TSLOG" >&2
  exit 4
fi

UP_ARGS=(--hostname="${TS_HOSTNAME_NETNS:-hidris-probe-netns-$(hostname -s)}"
         --accept-routes)
[[ -n "${TS_TAGS:-}" ]] && UP_ARGS+=(--advertise-tags="$TS_TAGS")

if ! tailscale --socket="$SOCKET" up --authkey="${TS_AUTHKEY:?}" \
      "${UP_ARGS[@]}" 2>&1 | sed 's/^/    /' >&2; then
  echo "netns: tailscale up failed; daemon log follows" >&2
  tail -30 "$TSLOG" >&2
  exit 5
fi

sleep 3
tailscale --socket="$SOCKET" status 2>&1 | sed 's/^/    /' >&2 || true

# The search domain is what makes a BARE name like "hidris-db" resolvable.
# Without it tailscaled does not recognise the query as a MagicDNS name,
# forwards it upstream to slirp4netns's resolver, and gets SERVFAIL — which is
# exactly how this failed before. Discovered rather than hardcoded so the file
# stays correct if the tailnet is renamed.
SUFFIX="$(tailscale --socket="$SOCKET" status --json 2>/dev/null \
          | python -c 'import json,sys; print(json.load(sys.stdin).get("MagicDNSSuffix",""))' \
          2>/dev/null)"
if [[ -n "$SUFFIX" ]]; then
  printf 'search %s\nnameserver 100.100.100.100\nnameserver 10.0.2.3\n' \
    "$SUFFIX" > "$WORKDIR/resolv.conf"
  echo "netns: resolv.conf search domain = $SUFFIX" >&2
else
  echo "netns: WARN could not determine MagicDNS suffix; bare names may fail" >&2
fi

# Wait for MagicDNS to actually answer before handing over to the probe.
# `tailscale up` returning does not mean the resolver is serving yet, and a
# probe that starts too early reports a DNS failure that is really a race.
for _ in $(seq 1 15); do
  getent hosts "${PROBE_DB_HOST:-hidris-db}" >/dev/null 2>&1 && break
  sleep 1
done
echo "netns: resolves ${PROBE_DB_HOST:-hidris-db} -> $(getent hosts "${PROBE_DB_HOST:-hidris-db}" | awk '{print $1}' | tr '\n' ' ')" >&2

# TS_SOCKET so probe.py's `tailscale status` reads THIS daemon, not the
# userspace one in the parent namespace.
TS_SOCKET="$SOCKET" PROBE_MODE=netns \
  python /opt/probe/probe.py > "$WORKDIR/probe-netns.json"
rc=$?
echo "netns: probe exited $rc" >&2
exit $rc
