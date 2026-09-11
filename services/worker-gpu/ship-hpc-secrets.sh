#!/usr/bin/env bash
# Push the credentials an HPC simulation job needs onto the login node, and
# verify they arrived byte-for-byte.
#
#   ./ship-hpc-secrets.sh
#
# Two secrets, deliberately carried by two different mechanisms:
#
#   pgpass      the CloudNativePG app password, as a libpq pgpass file.
#               It does NOT go in worker.env. Singularity 3.7.3 evaluates every
#               --env / --env-file / SINGULARITYENV_ value through a shell before
#               injecting it, so a password containing $ is silently truncated at
#               the $, and one containing " aborts the container outright with
#               "reached EOF without closing quote". There is no literal-passing
#               mode in 3.7.3. libpq reads a pgpass file itself with no shell in
#               the path, so that is the only reliable channel.
#
#   TS_AUTHKEY  the tailscale key, in worker.env. Safe there only because tskey
#               values are [A-Za-z0-9-] and survive the shell pass unchanged.
#
# The password travels on ssh's STDIN and is never a command argument, so it
# stays out of `ps` and out of any shell history. That is also the subtle part:
# the remote script therefore cannot be fed by heredoc, because a heredoc IS
# stdin and would take the password's place — the remote `bash -s` would read
# the script and `cat` would get nothing. It is base64'd into the command line
# instead, leaving stdin free for the one thing that must not appear there.
set -euo pipefail

: "${HPC_LOGIN:=jmormal@upvnet.upv.es}"
: "${HPC_HOST:=vrhpcadm1.dsic.upv.es}"
: "${HPC_KEY:=$HOME/.ssh/hidris_hpc}"
: "${PG_SECRET:=hidris-db-app}"
: "${PG_ROLE:=hidris}"
CONTAINERS="containers"

ssh_hpc() {
  ssh -i "$HPC_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR \
      "${HPC_LOGIN}@${HPC_HOST}" "$@"
}

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
[[ -r "$HPC_KEY" ]] || { echo "no ssh key at $HPC_KEY" >&2; exit 1; }

pg_password() {
  kubectl get secret "$PG_SECRET" -o jsonpath='{.data.password}' | base64 -d
}

echo "==> reading $PG_SECRET from $(kubectl config current-context)"
expected=$(pg_password | sha256sum | cut -c1-16)
expected_len=$(pg_password | wc -c)
[[ "$expected_len" -gt 0 ]] || { echo "!! secret $PG_SECRET has an empty password" >&2; exit 1; }
echo "    password: ${expected_len} bytes, sha256[0:16]=${expected}"

# Escaping for pgpass's own format (backslash and colon are the only special
# characters in a field) happens on the far side, so nothing has to survive a
# second shell on the way over.
REMOTE_B64=$(base64 -w0 <<'REMOTE'
set -euo pipefail
pw=$(cat)
if [ -z "$pw" ]; then
  echo "no password arrived on stdin" >&2
  exit 1
fi
mkdir -p "$HOME/$CONTAINERS"
esc=$(printf '%s' "$pw" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')
umask 077
printf '*:*:*:%s:%s\n' "$PG_ROLE" "$esc" > "$HOME/$CONTAINERS/pgpass"
chmod 600 "$HOME/$CONTAINERS/pgpass"
# Echo back a hash of what was actually received, so the caller can prove the
# bytes survived the trip rather than assuming they did.
printf '%s' "$pw" | sha256sum | cut -c1-16
REMOTE
)

echo "==> writing ~/$CONTAINERS/pgpass on $HPC_HOST"
actual=$(pg_password | ssh_hpc \
  "PG_ROLE='$PG_ROLE' CONTAINERS='$CONTAINERS' bash -c '
     s=\$(mktemp); printf %s $REMOTE_B64 | base64 -d > \$s;
     bash \$s; rc=\$?; rm -f \$s; exit \$rc'")

if [[ "$actual" != "$expected" ]]; then
  echo "!! pgpass mismatch: sent $expected, cluster received '${actual}'" >&2
  exit 1
fi
echo "    verified: cluster received sha256[0:16]=$actual"

echo "==> worker.env must NOT carry PG_PASSWORD (see header)"
if ssh_hpc "grep -q '^PG_PASSWORD=' ~/$CONTAINERS/worker.env 2>/dev/null"; then
  echo "    PG_PASSWORD still present in worker.env — removing"
  ssh_hpc "sed -i '/^PG_PASSWORD=/d' ~/$CONTAINERS/worker.env"
else
  echo "    ok, absent"
fi

echo "==> done"
