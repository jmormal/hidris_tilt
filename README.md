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
│   ├── redis.yaml             # We use redis for the management of the queues
│   ├── keda.yaml              # Keda autoscales the pods according to the jobs
│   └── mlflow.yaml            # We use mlflow to log the process and results of instances
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

Open <http://frontend.127.0.0.1.nip.io> in your browser.

## URLs

All traffic goes through port 80. Traefik routes by the `Host:` header.

| Service  | External URL                         | Internal (cluster) |
|----------|--------------------------------------|--------------------|
| frontend | <http://frontend.127.0.0.1.nip.io>     | <http://frontend:80> |
| api      | <http://api.127.0.0.1.nip.io>          | <http://api:80>      |

> The browser uses nip.io URLs. Services talking to *each other* use the short
> internal name — traffic stays inside the cluster and never touches Traefik.
