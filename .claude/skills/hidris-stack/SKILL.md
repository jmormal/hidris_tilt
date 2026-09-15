---
name: hidris-stack
description: >
  Operate the local Hidris dev stack — the k3d cluster, Tilt, KEDA workers, Postgres,
  Keycloak and the Tailscale ingresses. Use when bringing the stack up or down, when
  pods are Pending/CrashLooping/ErrImagePull, when config or secrets need to reach a
  pod, when a service needs a public tailnet URL, or when auth redirects break.
  Triggers: tilt up, k3d, pods pending, disk pressure, KEDA, scaledjob, worker not
  picking up jobs, keycloak redirect_uri, tailscale ingress, nip.io, add a service.
---

# Operating the local stack

```bash
./setup.sh      # once: k3d cluster "hidris" + local registry + GPU device plugin
tilt up         # build, deploy, live-sync
./devstart.sh   # tmux: nvim / tilt up / k9s
./teardown.sh   # delete the cluster
```

Tilt refuses any kube-context but `k3d-hidris` (`Tiltfile:4`) and fails fast if
`.env` is empty (`Tiltfile:47`).

## Config and secrets

`config.env` (committed, non-secret) and `.env` (gitignored, secret) are read by
`parse_env` and rendered into a ConfigMap and Secret that every pod picks up via
`envFrom`. Only values **identical across services** belong there; per-service
values stay in that service's manifest.

Two rules the Tiltfile learned the hard way, both in `env_to_yaml`
(`Tiltfile:33`):

- Values are emitted **single-quoted with `'` doubled**. A password containing a
  double quote otherwise corrupts the generated YAML and the failure surfaces as
  an unrelated manifest parse error.
- `parse_env` strips only a **matched** surrounding quote pair. Stripping
  unconditionally mangles any value that legitimately begins or ends with a quote.

**`.env` must never be committed — the superproject is a public GitHub repo.**
It is in `.gitignore` along with `/.env.*` (with `!.env.example`), and the
worker's filled-in `anuga.env` / `local.env` / `worker.env`, which carry
`PG_PASSWORD` and `TS_AUTHKEY`. Only `worker.env.example` belongs in git. Check
`git ls-files | grep -E '(^|/)\.env'` before any commit that touches config.

## Workers are run-once

KEDA watches Redis queue depth and spawns ephemeral Jobs from the worker images.
They must **pull one item, process it, exit** — `worker.work(burst=True)`. A
blocking `work()` leaves the Job running forever, KEDA never sees it finish, and
the queue appears stuck while a worker sits idle in it. `keda-gpu.yaml` also
carries `activeDeadlineSeconds: 14400` so a wedged solve cannot hold a GPU
indefinitely.

Queue names do not match their env vars: `REDIS_CPU` selects the **GPU** queue in
`main.py`, and `jobs:hpc` is deliberately separate so an HPC-bound job never
races KEDA for the same item.

## Pods Pending or evicted

Check `DiskPressure` before anything else:

```bash
kubectl describe node | grep -A3 Conditions
docker system df                      # build cache is invisible to kubelet image GC
docker builder prune -af              # 105 GB of it once wedged the whole cluster
```

The kubelet's image GC only sees images, not BuildKit's cache, so it evicts pods
while reporting plenty of reclaimable space. `ErrImagePull` is a separate
matter — `minio` has been stuck on it and is still outstanding.

An API pod hung at `Waiting for application startup` is almost always a Postgres
lock, not the API — see the `hpc-triage` skill.

## Tailnet exposure

Every public service carries **two** Ingresses: the Traefik one on
`<name>.127.0.0.1.nip.io` (local fallback) and a `tailscale` one annotated
`tailscale.com/hostname: "hidris-<name>"`, which the operator gives its own
device and a real Let's Encrypt cert at `hidris-<name>.tail51f978.ts.net`. No
mkcert CA and no `/etc/hosts` entry needed from any tailnet member.

When adding or renaming one:
- Keycloak must learn the new origin — `./keycloak-sync.sh` pushes redirect URIs
  and web origins. A missing entry shows up as `Invalid parameter: redirect_uri`
  at login, which looks like a broken client rather than a missing URL.
- Apps that validate Host headers need it too (mlflow's `--allowed-hosts` lists
  both the ts.net and nip.io names, with and without `:*`).
- `./tailnet-cleanup.sh` removes stale devices; every renamed ingress mints a
  new one.

## Adding a service

Follow the existing Tiltfile pattern: a `service_js`/`service_python`/
`service_image` call, a matching `k8s/<name>.yaml` with Deployment + Service +
Ingress (+ the `-ts` Ingress if it should be public), and
`resource_deps=["nip-tls"]` if it needs working HTTPS locally.

`worker-cpu`, `worker-gpu`, `worker-kpi`, `hpc-probe` and `jupyter` are plain
directories; `services/api` and `services/frontend` are **git submodules** with
their own remotes — commits there land in the submodule's repo, not this one.
