# microservices-demo

Three Node.js services running on k3d with Traefik routing — all on port 80, real URLs via nip.io, live file sync via Tilt.

## Structure

```
.
├── setup.sh                   # create the k3d cluster (run once)
├── teardown.sh                # delete the cluster
├── Tiltfile                   # live-reload dev loop
├── k8s/
│   ├── frontend.yaml          # Deployment + Service + Ingress
│   ├── api.yaml
│   └── auth.yaml
└── services/
    ├── frontend/              # serves a UI that calls api + auth
    │   ├── Dockerfile
    │   ├── package.json
    │   └── src/
    │       ├── index.js
    │       └── index.html
    ├── api/                   # REST API, calls auth internally
    │   ├── Dockerfile
    │   ├── package.json
    │   └── src/index.js
    └── auth/                  # validates tokens
        ├── Dockerfile
        ├── package.json
        └── src/index.js
```

## Prerequisites

```bash
brew install k3d tilt kubectl
```

## Getting started

```bash
# 1. Create the cluster (once)
./setup.sh

# 2. Start the dev loop — builds images, deploys, watches for changes
tilt up
```

Open http://frontend.127.0.0.1.nip.io in your browser.

## URLs

All traffic goes through port 80. Traefik routes by the `Host:` header.

| Service  | External URL                         | Internal (cluster) |
|----------|--------------------------------------|--------------------|
| frontend | http://frontend.127.0.0.1.nip.io     | http://frontend:80 |
| api      | http://api.127.0.0.1.nip.io          | http://api:80      |
| auth     | http://auth.127.0.0.1.nip.io         | http://auth:80     |

> The browser uses nip.io URLs. Services talking to *each other* use the short
> internal name — traffic stays inside the cluster and never touches Traefik.

## Kubernetes context

k3d creates a context named `k3d-dev` automatically.
The Tiltfile pins to it — Tilt will refuse to run against any other cluster.

```bash
kubectl config get-contexts          # list all contexts
kubectl config use-context k3d-dev   # switch manually
kubectl config current-context       # check active context
```

To use a different cluster name, update `allow_k8s_contexts(...)` in the Tiltfile.

## Live reload

Tilt syncs `src/` directly into the running container — no rebuild needed for
most changes. Node's `--watch` flag (built-in since v18) restarts the process
automatically when a synced file changes.

A full image rebuild only happens when you change the `Dockerfile` or `package.json`.

## Test tokens

The auth service accepts two hardcoded dev tokens:
- `dev-token-123`
- `dev-token-456`

Replace the `VALID_TOKENS` set in `services/auth/src/index.js` with real JWT logic.

## Teardown

```bash
./teardown.sh
```
# hidris_tilt
