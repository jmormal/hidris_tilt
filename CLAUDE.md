# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hidris (DANA Flood Viewer) — a digital twin of a river basin for flood simulation.
Users draw boundary conditions / operators on a map, submit an ANUGA hydrodynamic
simulation, watch it run via a live progress stream, and view the resulting flood
mesh animated over time. Runs on k3d (local Kubernetes) with Traefik routing and
Tilt for the live-reload dev loop. `ideas.md` (Spanish) has the product roadmap —
worth checking for the "why" behind in-progress features.

## Repository layout

This is a **superproject with git submodules**:

- `services/api` → https://github.com/jmormal/hidris_api.git
- `services/frontend` → https://github.com/jmormal/FloodViewer.git

Both have their own `.git`. When editing files inside them, commits happen in the
submodule's own repo, not the superproject — check `git status`/`git remote -v`
inside the submodule if unsure which repo you're in. `worker-cpu` and `worker-gpu`
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
    ├── worker-gpu/               # RQ worker, ANUGA on GPU (custom CUDA build)
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
   doesn't expect.
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
8. On completion the worker gzips the result and writes it directly into the
   `simulations.solution` BYTEA column (`db.save_solution` /
   `save_solution_bytes` — the GPU worker pre-gzips to avoid double
   compression), flipping `is_solved`. The frontend fetches it via
   `GET /api/instances/{id}/result`, which streams the gzip bytes back with
   `Content-Encoding: gzip` for the browser to inflate.
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

### Frontend architecture (`services/frontend`)

React + TypeScript + Vite + Tailwind v4, MapLibre GL for the basemap, deck.gl
for the GPU-accelerated flood mesh overlay. State is split across two React
contexts built with `useReducer`:

- `FloodProvider` / `FloodContext` — the flood dataset, playback (frame index,
  play/pause, speed), decoded frame cache.
- `SimulationProvider` / `SimulationContext` — the in-progress simulation
  setup (drawn polygons/boundary conditions, submission state).

`useFloodLayer` memoizes the deck.gl `SolidPolygonLayer` from decoded frame
data; `FloodMap` composes it onto MapLibre via `react-map-gl`'s
`MapboxOverlay`. `src/config/theme.ts` is the single source of truth for
map style, playback speeds, colors, and property labels — prefer editing it
over hardcoding values in components. Flood data frames are RLE-encoded and
decoded on demand through an LRU cache (`utils/decode.ts`) since full
per-frame mesh data would be too large to keep resident.

`src/utils/api.ts` is the typed client for all instance endpoints; every call
goes through `authFetch` so token refresh is never duplicated per-call.

### Data model (Postgres, via CloudNativePG)

Two independent schemas set up idempotently in `services/api/src/db.py`:

- `simulations` — one row per user "instance": `public_id` (UUID, the only ID
  ever exposed to clients/URLs — the `id` serial is internal),
  `user_id` scoped to the Keycloak `sub` claim (all queries filter by both
  `public_id` and `user_id`, so cross-user access is impossible by
  construction, not by an extra authz check), `instance` JSONB (the drawn
  setup), `solution` BYTEA (gzipped result), `is_solved`.
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
