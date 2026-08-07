#!/usr/bin/env bash
# Build hidris-probe.sif and rsync it to the cluster.
#
#   nix-shell -p singularity squashfsTools
#   ./build-sif.sh              # build, test locally, ship
#   SKIP_PUSH=1 ./build-sif.sh  # build and test only
#
# Same docker -> docker-daemon:// -> SIF path as worker-gpu/build-sif.sh, which
# is the conversion known to produce an image this cluster's Singularity 3.7.3
# accepts. Never add --oci: OCI-SIF is unreadable by 3.7.3.
set -euo pipefail

cd "$(dirname "$0")"

IMAGE_TAG="hidris-probe:latest"
SIF_OUT="hidris-probe.sif"
REMOTE="${REMOTE:-jmormal@upvnet.upv.es@vrhpcadm1}"
REMOTE_DIR="${REMOTE_DIR:-containers}"

# Small image, but the SIF conversion still wants real disk rather than tmpfs.
export SINGULARITY_TMPDIR="${SINGULARITY_TMPDIR:-$PWD/.sing-tmp}"
export SINGULARITY_CACHEDIR="${SINGULARITY_CACHEDIR:-$PWD/.sing-cache}"
mkdir -p "$SINGULARITY_TMPDIR" "$SINGULARITY_CACHEDIR"

echo "==> docker build"
docker build -f Dockerfile -t "$IMAGE_TAG" .

echo
echo "==> smoke test in docker (no tailnet — just checks the image is sane)"
docker run --rm --entrypoint bash "$IMAGE_TAG" -c '
  tailscaled --version | head -1
  proxychains4 --help 2>&1 | head -1 || true
  python -c "import psycopg2, pg8000, redis, socks; print(\"python deps OK\")"
  python -m py_compile /opt/probe/probe.py && echo "probe.py compiles"
'

echo
echo "==> converting to SIF (native mode)"
rm -f "$SIF_OUT"
singularity build "$SIF_OUT" "docker-daemon://${IMAGE_TAG}"

echo "==> confirming classic SIF, not OCI-SIF"
singularity sif list "$SIF_OUT"

ls -lh "$SIF_OUT"

if [[ -n "${SKIP_PUSH:-}" ]]; then
  echo "==> SKIP_PUSH set; not shipping"
  exit 0
fi

echo
echo "==> shipping to $REMOTE:~/$REMOTE_DIR/"
# rsync only creates the final path component, and only sometimes. Creating it
# explicitly also gets the mode right, since probe.env lives here.
ssh "$REMOTE" "mkdir -p ~/$REMOTE_DIR && chmod 700 ~/$REMOTE_DIR"
# probe.env is NOT synced: it holds TS_AUTHKEY and PG_PASSWORD. Copy it once by
# hand (chmod 600) so a rebuild can never overwrite the cluster's credentials
# with a stale local copy, or push an empty template over a working one.
rsync -avP --partial \
  "$SIF_OUT" run-probe.slurm \
  "${REMOTE}:${REMOTE_DIR}/"

cat <<NEXT

Shipped. First time only, create the env file on the cluster:

  ssh $REMOTE
  mkdir -p ~/$REMOTE_DIR && chmod 700 ~/$REMOTE_DIR
  vi ~/$REMOTE_DIR/probe.env      # from probe.env.example
  chmod 600 ~/$REMOTE_DIR/probe.env

Then run it, either from the login node:

  sbatch ~/$REMOTE_DIR/run-probe.slurm

or from the API (which does the same over SSH):

  curl -k -H "Authorization: Bearer \$TOKEN" \\
    -X POST https://api.127.0.0.1.nip.io/api/hpc/probe
NEXT
