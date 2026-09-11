#!/usr/bin/env bash
# Reclaim the tailnet hostnames the k8s operator actually wants.
#
#   ./tailnet-cleanup.sh            # show what would change
#   ./tailnet-cleanup.sh --apply    # do it
#
# The problem it fixes: when the k3d cluster is rebuilt, each operator proxy
# registers a NEW tailnet device. The old one lingers as an offline device still
# holding the name, so the new proxy is renamed with a numeric suffix —
# "hidris-db" stays with a dead device and the live proxy becomes "hidris-db-1".
# Every rebuild bumps the suffix again (tailscale-operator-1/-2/-3 got there
# already). Anything referring to these by MagicDNS name — notably worker.env on
# the HPC cluster — then points at a device that no longer answers.
#
# Two steps, in order:
#   1. delete the stale OFFLINE duplicates, freeing the base names;
#   2. delete each live proxy's state Secret and pod, so the operator re-auths
#      it and it claims the now-free name it was always annotated with.
#
# Step 2 is a brief tailnet outage for db/redis/api/frontend. Nothing in-cluster
# uses the tailnet path, so it only affects remote access and any running HPC
# job. Don't run it mid-simulation.
#
# Never touches user-owned devices (laptops, phones) or vrhpcadm1's own node.
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

ENVF="$(dirname "$0")/.env"
CID=$(sed -n 's/^TS_CLIENT_ID=//p' "$ENVF" | tr -d ' \r')
CSEC=$(sed -n 's/^TS_CLIENT_SECRET=//p' "$ENVF" | tr -d ' \r')
[[ -n "$CID" && -n "$CSEC" ]] || { echo "TS_CLIENT_ID/TS_CLIENT_SECRET missing from .env" >&2; exit 1; }

TOKEN=$(curl -fsS -u "$CID:$CSEC" -d 'grant_type=client_credentials' \
  https://api.tailscale.com/api/v2/oauth/token \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

api() { curl -fsS -X "$1" -H "Authorization: Bearer $TOKEN" "https://api.tailscale.com/api/v2$2"; }

# The two inline filters below are written to temp files rather than passed with
# python3 -c: they need single quotes for dict keys, and the whole thing would
# otherwise sit inside a single-quoted shell string.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/stale.py" <<'PYSTALE'
import json, re, sys

devs = json.load(sys.stdin)["devices"]
by_name = {d["name"].split(".")[0]: d for d in devs}


def seen(d):
    return d.get("lastSeen", "")


for name, d in sorted(by_name.items()):
    # Operator-managed only. A user device never gets auto-suffixed like this,
    # and an untagged device is somebody's laptop.
    if not any(t.startswith("tag:k8s") for t in d.get("tags", [])):
        continue
    # Never touch something currently talking to the control plane, whatever
    # the names suggest. There is no "online" field on this endpoint —
    # connectedToControl is the one that means it.
    if d.get("connectedToControl"):
        continue
    # The signal is the pairing: a numeric-suffixed sibling is what proves the
    # operator wanted this name and could not have it. Require the sibling to
    # be strictly FRESHER, so if the live proxy is ever the base name we delete
    # the suffixed leftover rather than the device in use.
    siblings = [
        o for n, o in by_name.items()
        if re.fullmatch(re.escape(name) + r"-\d+", n)
    ]
    if not siblings:
        continue
    if not any(seen(o) > seen(d) for o in siblings):
        continue
    print(d["id"] + "\t" + name)
PYSTALE

cat > "$TMP/names.py" <<'PYNAMES'
import json, sys

for d in sorted(json.load(sys.stdin)["devices"], key=lambda x: x["name"]):
    if any(t.startswith("tag:k8s") for t in d.get("tags", [])):
        print("    {:<24} online={}".format(d["name"].split(".")[0], d.get("online")))
PYNAMES

cat > "$TMP/live.py" <<'PYLIVE'
import json, sys

for d in json.load(sys.stdin)["devices"]:
    tags = d.get("tags", [])
    # Operator proxies only. tag:k8s-operator is the operator's own device and
    # is reset the same way; tag:hpc (compute-node workers) and untagged user
    # machines are never touched.
    if not any(t.startswith("tag:k8s") for t in tags):
        continue
    print(d["id"] + "\t" + d["name"].split(".")[0])
PYLIVE

echo "==> finding stale duplicates"
mapfile -t VICTIMS < <(api GET /tailnet/-/devices | python3 "$TMP/stale.py")

if [[ ${#VICTIMS[@]} -eq 0 ]]; then
  echo "    none — names are already clean"
else
  for v in "${VICTIMS[@]}"; do echo "    stale: ${v#*$'\t'} (${v%%$'\t'*})"; done
fi

if [[ $APPLY -eq 0 ]]; then
  echo
  echo "dry run. re-run with --apply to delete these and re-register the proxies."
  exit 0
fi

for v in "${VICTIMS[@]}"; do
  id="${v%%$'\t'*}"; name="${v#*$'\t'}"
  api DELETE "/device/$id" >/dev/null && echo "    deleted $name"
done

echo "==> clearing the live proxy devices too"
# Not optional, and the order matters. A proxy's identity is its state Secret;
# drop that alone and it re-registers as a BRAND NEW device while the old one
# still holds the name — which recreates the exact drift this script exists to
# fix, one suffix higher. So the live device has to go at the same time, which
# leaves every annotated name free for the proxy that wants it.
#
# Only tag:k8s devices, so the HPC workers (tag:hpc) and user machines are
# never in scope.
mapfile -t LIVE < <(api GET /tailnet/-/devices | python3 "$TMP/live.py")
for v in "${LIVE[@]}"; do
  id="${v%%$'\t'*}"; name="${v#*$'\t'}"
  api DELETE "/device/$id" >/dev/null && echo "    cleared $name"
done

echo "==> resetting proxies"
for sts in $(kubectl get statefulset -n tailscale -o name 2>/dev/null | sed 's|.*/||'); do
  kubectl delete secret -n tailscale "${sts}-0" --ignore-not-found >/dev/null
  kubectl delete pod -n tailscale "${sts}-0" --ignore-not-found >/dev/null
  echo "    reset $sts"
done

echo "==> waiting for re-registration"
for _ in $(seq 1 40); do
  sleep 5
  total=$(kubectl get statefulset -n tailscale --no-headers 2>/dev/null | wc -l)
  ready=$(kubectl get pod -n tailscale --no-headers 2>/dev/null | grep -c "1/1 *Running" || true)
  [[ "$total" -gt 0 && "$ready" -ge $((total + 1)) ]] && break
done
kubectl get pod -n tailscale

echo
echo "==> names now"
api GET /tailnet/-/devices | python3 "$TMP/names.py"
echo
echo "If the names lost their suffix, update ~/containers/worker.env on the HPC"
echo "cluster: DB_HOST=hidris-db and REDIS_URL=redis://hidris-redis:6379"
