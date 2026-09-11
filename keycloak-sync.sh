#!/usr/bin/env bash
# Reconcile the hidris-frontend client with k8s/keycloak-realm.yaml.
#
# Why this exists: `start-dev --import-realm` only imports a realm that does not
# already exist. Once the realm is in the keycloak Postgres — which it is, and
# which survives `tilt down` — editing the ConfigMap changes nothing at all, and
# the symptom is "Invalid parameter: redirect_uri" at login rather than anything
# pointing at the import. So the declarative file stays the source of truth and
# this pushes the parts that actually drift.
#
# It also folds in whatever the tailscale Ingresses report RIGHT NOW. Those can
# disagree with the declared names: when a stale offline device still holds a
# name, the operator gives the live proxy a numeric suffix (hidris-dt ->
# hidris-dt-1), and the real frontend origin is then missing from the list.
# ./tailnet-cleanup.sh fixes the drift; this keeps login working meanwhile.
#
# Idempotent — Tilt runs it on every keycloak restart.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REALM_FILE="$HERE/k8s/keycloak-realm.yaml"
REALM=hidris
CLIENT=hidris-frontend

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/declared.py" <<'PY'
import json, re, sys

text = open(sys.argv[1]).read()
body = re.search(r"hidris-realm\.json: \|\n(.*)", text, re.S).group(1)
realm = json.loads("\n".join(line[4:] for line in body.split("\n")))
client = next(c for c in realm["clients"] if c["clientId"] == "hidris-frontend")
print(json.dumps({
    "redirects": client["redirectUris"],
    "origins": client["webOrigins"],
}))
PY

cat > "$TMP/live.py" <<'PY'
import json, sys

redirects, origins = [], []
for ing in json.load(sys.stdin)["items"]:
    if ing["spec"].get("ingressClassName") != "tailscale":
        continue
    lb = ing.get("status", {}).get("loadBalancer", {}).get("ingress") or []
    host = lb[0].get("hostname") if lb else None
    if not host:
        continue
    origins.append("https://" + host)
    # The api only ever redirects to its Swagger helper; everything else is a
    # browser app served at the root.
    if ing["metadata"]["name"].startswith("api"):
        redirects.append("https://" + host + "/docs/oauth2-redirect")
    else:
        redirects.append("https://" + host + "/*")
print(json.dumps({"redirects": redirects, "origins": origins}))
PY

cat > "$TMP/merge.py" <<'PY'
import json, sys

declared = json.loads(sys.argv[1])
live = json.loads(sys.argv[2])


def merge(key):
    out = []
    for x in declared[key] + live[key]:
        if x not in out:
            out.append(x)
    return out


# Compact, so each list survives as a single shell word.
print(json.dumps(merge("redirects"), separators=(",", ":")),
      json.dumps(merge("origins"), separators=(",", ":")))
PY

DECLARED=$(python3 "$TMP/declared.py" "$REALM_FILE")
LIVE=$(kubectl get ingress -A -o json 2>/dev/null | python3 "$TMP/live.py")
[[ -n "$LIVE" ]] || LIVE='{"redirects":[],"origins":[]}'
read -r REDIRECTS ORIGINS <<<"$(python3 "$TMP/merge.py" "$DECLARED" "$LIVE")"

POD=$(kubectl get pod -l app=keycloak -o jsonpath='{.items[0].metadata.name}')
[[ -n "$POD" ]] || { echo "no keycloak pod" >&2; exit 1; }

kc() { kubectl exec -i "$POD" -- /opt/keycloak/bin/kcadm.sh "$@"; }

kc config credentials --server http://localhost:8080 \
   --realm master --user admin --password admin >/dev/null

ID=$(kc get clients -r "$REALM" -q "clientId=$CLIENT" --fields id --format csv --noquotes | tr -d '\r')
[[ -n "$ID" ]] || { echo "client $CLIENT not found in realm $REALM" >&2; exit 1; }

kc update "clients/$ID" -r "$REALM" \
  -s "redirectUris=$REDIRECTS" \
  -s "webOrigins=$ORIGINS"

echo "keycloak: $CLIENT reconciled"
kc get "clients/$ID" -r "$REALM" --fields 'redirectUris,webOrigins'
