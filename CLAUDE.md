# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hidris (DANA Flood Viewer) — a digital twin of a river basin for flood simulation.
Users draw boundary conditions / operators on a map, submit an ANUGA hydrodynamic
simulation, watch it run via a live progress stream, and view the resulting flood
mesh animated over time. Runs on k3d (local Kubernetes) with Traefik routing and
Tilt for the live-reload dev loop. `ideas.md` (Spanish) has the product roadmap —
worth checking for the "why" behind in-progress features.

For deep dives beyond this file's overview, `docs/` has three long-form,
file-and-line-referenced write-ups: `docs/architecture.md` (full job lifecycle,
auth, data model, infra — read this first), `docs/frontend-react.md` (a React
tour of the frontend, concept-by-concept), and `docs/solution-binary-format.md`
(the `HFR1` binary container spec). They're worth reading directly rather than
summarized here — each also lists known dead ends and stale leftovers in the
code. The root `README.md` is stale (describes an old three-service Node.js
prototype) — ignore it.

`.claude/skills/` holds four operational skills for the recurring workflows, each
carrying the failure modes that cost real time to diagnose: `hpc-solve`
(submit and follow a Slurm run), `hpc-triage` (a job failed, went quiet, or will
not start), `hpc-image` (change what actually runs on the cluster — the SIF and
the bind-mounted overrides that currently supersede it), and `hidris-stack` (the
local k3d/Tilt/KEDA loop). Read the relevant one before touching that area;
`docs/` explains how the system is built, the skills explain how it is operated.

## Repository layout

This is a **superproject with git submodules**:

- `services/api` → https://github.com/jmormal/hidris_api.git
- `services/frontend` → https://github.com/jmormal/FloodViewer.git

Both have their own `.git`. When editing files inside them, commits happen in the
submodule's own repo, not the superproject — check `git status`/`git remote -v`
inside the submodule if unsure which repo you're in. `worker-cpu`, `worker-gpu`, `hpc-probe`, and `jupyter`
are NOT submodules (plain directories in the main repo).

```
.
├── config.env, .env            # shared config (config.env committed, .env secret/gitignored)
├── Tiltfile                     # orchestrates the whole dev stack (see below)
├── setup.sh / teardown.sh       # create/delete the k3d cluster
├── devstart.sh                  # tmux session: nvim + tilt up + k9s
├── k8s/*.yaml                   # one manifest per service/piece of infra
└── services/
    ├── frontend/                # React/TS UI — submodule
    ├── api/                     # FastAPI REST API — submodule
    ├── worker-cpu/               # RQ worker, ANUGA on CPU
    ├── worker-gpu/               # RQ worker, ANUGA on GPU (custom CUDA build); also the
    │                             #   source for the Singularity image used on external HPC
    ├── hpc-probe/                # tiny SIF that smoke-tests the Tailscale path to an
    │                             #   external HPC cluster (see "HPC execution path" below)
    └── jupyter/                  # notebook environment
```

## Common commands

### Cluster / dev loop (from repo root)

```bash
./setup.sh          # one-time: create k3d cluster "hidris" + local registry + GPU device plugin
tilt up              # build all images, deploy to k8s, live-sync on file change
./teardown.sh        # delete the k3d cluster
./devstart.sh         # tmux session with nvim / tilt up / k9s panes
```

Tilt refuses to run against any kube-context other than `k3d-hidris`
(`allow_k8s_contexts` in the Tiltfile). Prereqs: `k3d`, `tilt`, `kubectl`, and a
one-time `mkcert -install` on the host (the Tiltfile signs a wildcard cert for
`*.127.0.0.1.nip.io` on every `tilt up`).

All service URLs are `<name>.127.0.0.1.nip.io` over HTTPS, routed by Traefik on
the Host header (frontend, api, keycloak, jupyter, minio, mlflow, pgadmin).
Service-to-service calls inside the cluster use the short name instead
(e.g. api → `hidris-db-rw`, `redis`, `keycloak.default.svc.cluster.local`).

### Frontend (`services/frontend`)

```bash
npm install
npm run dev      # vite dev server, but normally driven by Tilt, not run standalone
npm run build    # tsc -b && vite build
npm run preview
```

### API (`services/api`)

```bash
uvicorn src.main:app --reload --port 8000     # requires Redis + at least one RQ worker running
rq worker jobs:cpu --url redis://localhost:6379
python src/db.py     # idempotent: creates the simulations table + storm raster schema
```

No test suite, linter, or CI config exists in this repo currently — don't assume
one; check with the user before adding tooling conventions.

## Configuration model

Two files at the repo root feed every pod via `envFrom` (see Tiltfile):

- `config.env` — non-secret, shared across services, **committed**. Only values
  identical across services belong here (Redis URL, queue names, MLflow URI).
- `.env` — secret values, **gitignored**. Copy from `.env.example` if present;
  Tilt fails fast (`fail(...)`) if this is empty.

Per-service values (e.g. which queue a specific worker drains) stay inline in
that service's k8s manifest/pod spec, not in these shared files.

## Architecture

### Request / job flow

1. **Frontend** authenticates via Keycloak (`keycloak-js`, realm `hidris`,
   client `hidris-frontend`) and calls the API through `authFetch`
   (`services/frontend/src/auth/keycloak.ts`), which attaches a bearer token
   and refreshes it when <30s of life remain.
2. **API** (`services/api/src/main.py`, FastAPI) validates that token itself —
   it does not issue tokens, only verifies the RS256 signature against
   Keycloak's JWKS and checks the issuer (`src/auth.py`). Two auth
   dependencies exist: `current_user` (Authorization header, normal routes)
   and `current_user_from_query` (`?access_token=`, SSE only — `EventSource`
   can't set headers). Both funnel through the same `_validate_token`.
3. Simulation "instances" (name, description, a JSON `instance` payload of
   boundary conditions/operators) are CRUD'd against Postgres
   (`services/api/src/db.py`, CloudNativePG cluster `hidris-db`). Editing an
   instance's config is meant to invalidate any existing solution (see the
   commented-out `is_solved`/`solution` reset in `update_instance` — currently
   disabled, worth confirming intent before relying on it).
4. `POST /api/instances/{id}/simulate` enqueues an RQ job
   (`tasks.run_anuga`) onto a Redis queue (`jobs:gpu` by default via
   `REDIS_CPU` env var — note the name mismatch, it actually selects the GPU
   queue in `main.py`).
5. **KEDA** watches Redis queue depth and spawns ephemeral Kubernetes Jobs
   from the `worker-cpu`/`worker-gpu` images to drain `jobs:cpu`/`jobs:gpu`
   (`k8s/keda-cpu.yaml`, `k8s/keda-gpu.yaml`). Workers are **run-once**: pull
   one item, process, exit — they must never loop internally in a way KEDA
   doesn't expect. This is one of two execution paths — see "HPC execution
   path" below for the other.
6. Workers publish progress over Redis pub/sub using the shared contract in
   `events.py` (duplicated identically in `services/api/src/events.py` and
   each worker's `src/events.py` — keep them in sync if the event shape
   changes). Channel: `sim:events:{job_id}`. Payload: `{"event": ..., "data":
   {...}}`, events are `queued | meshing | progress | complete | error`.
7. The API relays these over Server-Sent Events at
   `/api/simulate/{job_id}/stream` — a background thread reads from a
   blocking `pubsub.get_message`, hands off to the asyncio loop via
   `call_soon_threadsafe`, with a heartbeat comment every `SSE_HEARTBEAT`
   seconds to keep the connection alive.
8. On completion the worker gzips the result and writes it into a Postgres
   large object via `db.save_solution_bytes` (the GPU worker pre-gzips to
   avoid double compression), flipping `is_solved` — see "Data model" below
   for why it's a large object rather than an inline column. The frontend
   fetches it via `GET /api/instances/{id}/result`, which streams the gzip
   bytes back with `Content-Encoding: gzip` for the browser to inflate.
9. Job *status* (as opposed to result data) is polled via `/job-status/{id}`
   (RQ registries: queued/started/finished/failed/scheduled) or read from
   `job.meta` (`progress`, `status_message`) — the SSE stream is the intended
   live path, the polling endpoint is a fallback/single-shot check.

### Worker internals (`worker-cpu`, `worker-gpu`)

Both use an identical `SpotGracefulWorker` (subclass of `rq.Worker`) that
intercepts SIGTERM (sent by Kubernetes on node drain / spot reclaim / KEDA
scale-down / rolling update) and **re-queues the in-flight job** rather than
losing it, up to `MAX_SPOT_RETRIES` (default 3, tracked in `job.meta`). Past
that it marks the job failed with a status message instead of just dying
silently. If you touch retry/termination logic, change it in both worker
directories — they are not shared code, just parallel copies.

`worker-gpu`'s Dockerfile builds ANUGA from source
(`anuga-community/anuga_core`, `develop` branch) against a specific CUDA
toolchain (`nvcr.io/nvidia/nvhpc:24.7-devel-cuda_multi-ubuntu22.04`) targeting
`gpu_arch=cc89` — this is a slow, heavy build; the Tiltfile's
`worker_build(..., live_pip=False)` intentionally skips syncing
`requirements.txt` changes into a running container for this image (full
rebuild required) because it sits on top of a multi-GB compiled base.

### HPC execution path (in progress)

Beyond the k3d/KEDA path above, `POST /api/instances/{id}/simulate` accepts a
`target: "cluster" | "hpc" | "slurm"` field (`services/api/src/main.py`,
default `"cluster"`). `"hpc"`/`"slurm"` submit the same simulation to an
**external Slurm cluster** instead of enqueuing for KEDA:

- `services/api/src/hpc.py` runs Slurm commands (`sbatch`, job-state queries)
  over SSH via `src/remote.py`, validating every interpolated id (Slurm job id
  or UUID) before it reaches a remote shell.
- The cluster reaches this stack's Redis/Postgres over a **Tailscale tailnet**
  from inside the job (`services/worker-gpu/hpc-entrypoint.sh`,
  `netns-run.sh`) — compute nodes lack `CAP_NET_ADMIN`, so `tailscaled` runs
  userspace and the job's network namespace is bridged onto it. `hpc_run.py`
  (`services/worker-gpu/src/hpc_run.py`) then runs the same ANUGA solve logic
  and publishes to the same `sim:events:{job_id}` channel, so the frontend's
  existing SSE stream works unmodified for either target.
- The worker runs from a Singularity/SIF image built from the `worker-gpu`
  Dockerfile — see `services/worker-gpu/HPC-RESUME.md` for the build/ship
  procedure and current state (**as of the last update, this path has never
  been run end-to-end** — treat it as unverified, not production).
- `services/hpc-probe` is a small, fast-building SIF used to test just the
  tailnet connectivity question (which transport reaches Redis/Postgres from
  a compute node) in isolation from the multi-GB `worker-gpu` image.
- `GET /api/hpc/simulation/{slurm_job_id}/log` is the diagnostic when SSE goes
  quiet — a job that dies before the tailnet comes up can't reach Redis to
  report anything, so the Slurm `.out` log is the only evidence.
- The queue name mismatch matters here too: `q_hpc` uses `jobs:hpc`
  (`QUEUE_HPC` env var), a separate queue from KEDA's `jobs:gpu`/`jobs:cpu`,
  specifically so an HPC-drained job never races KEDA for the same item.

### Frontend architecture (`services/frontend`)

React + TypeScript + Vite + Tailwind v4, MapLibre GL for the basemap, deck.gl
for the GPU-accelerated flood mesh overlay, plus a `/3d/:id` route
(`pages/Instance3D.tsx`) that renders the same solution as real terrain/water
geometry rather than a flat overlay. State is split across two React
contexts, each further split into a state context and an actions context
(`useReducer` underneath) so components that only dispatch don't re-render
on every playback tick:

- `FloodProvider` / `FloodContext` — the flood dataset, playback (frame index,
  play/pause, speed), precomputed per-frame color buffers.
- `SimulationProvider` / `SimulationContext` — the in-progress simulation
  setup (drawn polygons/boundary conditions, submission state, SSE stream).

The flood mesh layer (a deck.gl `SolidPolygonLayer`) is built inline in
`FloodMap.tsx`, memoized with `useMemo`/`updateTriggers` — there is no
separate `useFloodLayer` hook. `src/config/theme.ts` and
`src/config/polygonTypes.ts` are the sources of truth for map style,
playback speeds, colors/labels, and polygon-type definitions (properties,
draw behavior) respectively — prefer editing these registries over
hardcoding values in components. The solution format is a custom binary
container (`HFR1`, see `docs/solution-binary-format.md`), not JSON — a large
run's JSON would exceed V8's ~512MB single-string cap. `utils/decode.ts` and
related RLE-era stubs are dead code kept only so old imports resolve; the
real path is `utils/decodeResult.ts`.

`src/utils/api.ts` is the typed client for all instance endpoints; every call
goes through `authFetch` so token refresh is never duplicated per-call.

### Data model (Postgres, via CloudNativePG)

Two independent schemas set up idempotently in `services/api/src/db.py`:

- `simulations` — one row per user "instance": `public_id` (UUID, the only ID
  ever exposed to clients/URLs — the `id` serial is internal),
  `user_id` scoped to the Keycloak `sub` claim (all queries filter by both
  `public_id` and `user_id`, so cross-user access is impossible by
  construction, not by an extra authz check), `instance` JSONB (the drawn
  setup), `is_solved`. The gzipped result itself lives in a Postgres **large
  object** referenced by `solution_oid` (`db.save_solution_bytes` writes it in
  8 MiB chunks — an inline `bytea` parameter over ~500MB gets rejected by
  Postgres because psycopg2 hex-escapes it into the SQL text, doubling the
  size); a legacy inline `solution` BYTEA column is still read as a fallback
  for rows written before this change.
- `storm_catalog` / `storm_raster_data` — historical storm rainfall rasters
  (PostGIS `postgis`/`postgis_raster` extensions), served as tiles via Martin
  (`k8s/martin.yaml`). SQL functions for tile/value lookups live in
  `services/api/src/02_functions.sql`, loaded separately via
  `db.load_storm_functions()` since they change independently of the schema.

### Infra pieces (all wired in the Tiltfile, one block per piece)

- **KEDA** — installed via Helm, watches Redis queue depth, creates
  `ScaledJob`s that spawn worker Jobs on demand (not a long-running
  Deployment).
- **CloudNativePG** — Postgres operator (Helm), manages the `hidris-db`
  cluster; pgAdmin's connection secret is derived from the CNPG-generated app
  password via a `local_resource` (shell, runs on host) rather than being
  static.
- **Keycloak** — realm config (`k8s/keycloak-realm.yaml`) applied as a
  ConfigMap before the Keycloak deployment starts.
- **Martin** — vector/raster tile server backing storm data visualization.
- **MLflow / MinIO** — experiment tracking + S3-compatible object storage,
  configured via the shared `config.env` (endpoint URLs are non-secret;
  credentials live in `.env`).

When adding a new service to the stack, follow the existing pattern in the
Tiltfile: a `service_js`/`service_python`/`service_image` helper call, a
matching `k8s/<name>.yaml` with Deployment+Service+Ingress, and a
`resource_deps=["nip-tls"]` if it needs a working HTTPS ingress.
