# Resuming the HPC worker rollout

Paused partway through because the SIF transfer is ~10–15GB and should not go
over mobile data. **Do the remaining steps on ethernet.**

Nothing here needs the network *except* step 2 (rsync). The build reads from the
local docker daemon — it uses no bandwidth at all.

## Already done — do not redo

| what | where | state |
|---|---|---|
| `anuga-gpu:hpc` docker image (ANUGA + tailscale + proxychains + slirp4netns) | local docker | **built**, 36.9GB, layers cached |
| `hpc-entrypoint.sh`, `netns-run.sh`, `src/hpc_run.py`, `run-simulation.slurm` | this dir | written, syntax-checked, baked into the image |
| `get_instance_by_public_id` | `src/db.py` | added |
| API: `?target=hpc`, `/api/hpc/simulation/{id}/log`, SSE tolerating non-RQ jobs | `services/api` | live, routes verified in `/openapi.json` |
| Frontend: Cluster/HPC toggle, `submitSimulation(target)` | `services/frontend` | typechecks clean |
| `run-simulation.slurm`, `worker.env.example` | `~/containers/` on cluster | **shipped** |

## Step 1 — build the SIF (no network, ~30–60 min)

`/tmp` is a 16GB tmpfs and the image is 36.9GB, so **`TMPDIR` must point at real
disk** — this is what killed the first attempt (`disk quota exceeded`). Setting
only `SINGULARITY_TMPDIR` is not enough; singularity's OCI fetch uses plain
`TMPDIR` too.

```bash
cd services/worker-gpu
rm -rf .sing-tmp && mkdir -p .sing-tmp .sing-cache   # stale staging is not resumable

TMPDIR="$PWD/.sing-tmp" \
SINGULARITY_TMPDIR="$PWD/.sing-tmp" \
SINGULARITY_CACHEDIR="$PWD/.sing-cache" \
nix-shell -p singularity squashfsTools --run \
  "TMPDIR=$PWD/.sing-tmp singularity build anuga-gpu.sif docker-daemon://anuga-gpu:hpc"
```

Needs ~40–70GB free transiently. Watch with
`du -sh .sing-tmp; ls -lh anuga-gpu.sif`.

Then **confirm it is a classic SIF, not OCI-SIF** — Singularity 3.7.3 on the
cluster cannot read OCI-SIF, and discovering that after a 4-hour upload is
avoidable:

```bash
nix-shell -p singularity --run "singularity sif list anuga-gpu.sif"
# expect a "FS (Squashfs/*System/amd64)" partition, no OCI blobs
```

## Step 2 — ship it (ETHERNET; ~10–15GB)

```bash
rsync -avP --partial anuga-gpu.sif \
  jmormal@upvnet.upv.es@vrhpcadm1:containers/
```

`--partial` matters: if it drops, rerunning resumes rather than restarting.

## Step 3 — worker.env on the cluster (one-off)

Same credentials as `probe.env`, plus the ANUGA settings. From this machine:

```bash
TS_SECRET='tskey-...'                 # same key the probe uses
PGPW=$(kubectl get secret hidris-db-app -o jsonpath='{.data.password}' | base64 -d)

ssh jmormal@upvnet.upv.es@vrhpcadm1 \
  'cat > ~/containers/worker.env && chmod 600 ~/containers/worker.env' <<EOF
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
OMP_NUM_THREADS=1
CUDA_CACHE_PATH=/scratch/nv-cache
TS_AUTHKEY=$TS_SECRET
TS_TAGS=tag:hpc
REDIS_URL=redis://hidris-redis:6379
DB_HOST=hidris-db
DB_PORT=5432
DB_NAME=hidris
PG_USER=hidris
PG_PASSWORD=$PGPW
ANUGA_ELEVATION=/app/src/mi_terreno_whole.tif
ANUGA_GPU_SUBPROCESS=0
ANUGA_GPU_MODE=2
TMPDIR=/scratch/tmp
EOF
```

## Step 4 — first run

```bash
# from the cluster, to see the raw output
ssh jmormal@upvnet.upv.es@vrhpcadm1
cd ~/containers && PUBLIC_ID=<uuid> JOB_ID=$(uuidgen) sbatch \
  --export=ALL,PUBLIC_ID=$PUBLIC_ID,JOB_ID=$JOB_ID run-simulation.slurm
```

or from the UI: pick **HPC** on the solve toggle in the simulation panel.

Progress streams over the existing SSE endpoint either way — `hpc_run.py`
publishes the same `events.py` shapes to `sim:events:{JOB_ID}`.

## What is untested

The whole HPC simulation path has never run. Verified so far: the SIF *concept*
(hpc-probe proved tailnet + Redis + Postgres reach from vrhpc1, with `netns`
giving a real TUN so psycopg2 needs no proxy), and that every file compiles.

Not verified: ANUGA actually running under `--nv` in this image, `hpc_run.py`
end to end, and the frontend toggle in a browser. Expect the first run to need
iteration — start with a small instance.

First thing to check if it fails: `~/containers/hidris-sim-<jobid>.out`, or
`GET /api/hpc/simulation/<slurm_id>/log`. A job that dies before the tailnet is
up cannot reach Redis to report it, so SSE will simply stay silent — the Slurm
log is the only evidence in that window.
