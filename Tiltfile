# ── Context guard ─────────────────────────────────────────────────────────────
# Tilt refuses to run against any context other than k3d-dev.
# If you named your cluster differently, update this string.
allow_k8s_contexts("k3d-hidris")

load("ext://helm_resource", "helm_resource", "helm_repo")

# ── KEDA Custom Resource Support ──────────────────────────────────────────────
# Teach Tilt where to inject the dynamically built image tags in ScaledJobs
k8s_kind("ScaledJob", image_json_path="{.spec.jobTargetRef.template.spec.containers[*].image}")

# ── Config & secrets ──────────────────────────────────────────────────────────
# Externalized env lives in two files at the repo root:
#   config.env  -> non-secret shared config (committed)  -> ConfigMap app-config
#   .env        -> secret values (gitignored)            -> Secret    app-secrets
# Change a host/url/credential in ONE place; every service picks it up via
# envFrom. Per-service values (e.g. a worker's QUEUE) stay inline in the pod.

def parse_env(text):
    out = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def env_to_yaml(d):
    return "".join(['  {}: "{}"\n'.format(k, v) for k, v in d.items()])

config_vars = parse_env(read_file("./config.env", default=""))
secret_vars = parse_env(read_file("./.env", default=""))

if not secret_vars:
    fail("No secrets found. Copy .env.example to .env and fill it in.")

k8s_yaml(blob("""apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
""" + env_to_yaml(config_vars)))

k8s_yaml(blob("""apiVersion: v1
kind: Secret
metadata:
  name: app-secrets
type: Opaque
stringData:
""" + env_to_yaml(secret_vars)))

# ── Helper: build + deploy one service ────────────────────────────────────────
def service_js(name, port):
    docker_build(
        name,
        "./services/" + name,
        entrypoint=["npm", "run", "dev"],
        live_update=[
            sync("./services/frontend/src", "/app/src"),
            sync("./services/frontend/public", "/app/public"),
            run(
                "cd /app && npm install",
                trigger=["./services/frontend/package.json"],
            ),
        ],
    )
    k8s_yaml("./k8s/" + name + ".yaml")
    k8s_resource(
        name,
        links=[link("http://" + name + ".127.0.0.1.nip.io", name)],
    )

# ── Helper: build + deploy one service ────────────────────────────────────────
def service_python(name, port):
    docker_build(
        name,
        "./services/" + name,
        entrypoint=["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", str(port), "--reload"],
        live_update=[
            sync("./services/" + name + "/src", "/app/src"),
            run(
                "cd /app && pip install -r requirements.txt",
                trigger=["./services/" + name + "/requirements.txt"],
            ),
        ],
    )
    k8s_yaml("./k8s/" + name + ".yaml")
    k8s_resource(
        name,
        links=[link("http://" + name + ".127.0.0.1.nip.io", name)],
    )

# ── Helper: build a worker image (used by KEDA ScaledJobs) ────────────────────
# One image per worker type (cpu/gpu). Each folder has its own Dockerfile that
# defines the ENTRYPOINT, so we don't override it here. Workers run RUN-ONCE:
# pull one item from QUEUE, process it, exit 0 — they must NOT loop.
#
# live_pip=False for heavy images (e.g. the ANUGA GPU build): only sync source
# on change, never rerun pip in-cluster. A requirements.txt change then needs a
# full rebuild (correct — it sits on top of a multi-GB compiled base).
def worker_build(image, src, live_pip=True):
    docker_build(image, src)

# ── Helper: deploy a prebuilt off-the-shelf image ─────────────────────────────
def service_image(name, links=[]):
    k8s_yaml("./k8s/" + name + ".yaml")
    k8s_resource(name, links=links)

# ── Services ──────────────────────────────────────────────────────────────────
service_js("frontend", 3000)
service_python("api",      8080)

# Jupyter
#
docker_build(
    "jupyter",
    "./services/jupyter",
    live_update=[
        run(
            "cd /app && pip install -r requirements.txt",
            trigger=["./services/jupyter/requirements.txt"],
        ),
    ],
)
k8s_yaml("./k8s/jupyter.yaml")
k8s_resource(
    workload="jupyter",
    new_name="jupyter",
    links=[link("http://jupyter.127.0.0.1.nip.io", "jupyter")],
)

# ── Workers ───────────────────────────────────────────────────────────────────
# Two images, two folders, two Dockerfiles. CPU is slim; GPU carries CUDA+torch.
# No k8s_resource here — KEDA creates ephemeral Jobs from these images on demand
# (worker-cpu <- jobs:cpu, worker-gpu <- jobs:gpu). See keda-*.yaml below.
worker_build("worker-cpu", "./services/worker-cpu")
worker_build("worker-gpu", "./services/worker-gpu", live_pip=False)

# ── Infra (prebuilt images) ───────────────────────────────────────────────────
service_image("minio", links=[link("http://minio.127.0.0.1.nip.io", "minio console")])
service_image("redis")
service_image("mlflow", links=[link("http://mlflow.127.0.0.1.nip.io", "mlflow")])

# ── KEDA (cluster-wide autoscaler operator) ───────────────────────────────────
# KEDA is an operator, not a single Deployment — install it via Helm, then
# apply ScaledJobs that spawn worker Jobs on demand.
helm_repo("kedacore", "https://kedacore.github.io/charts")
helm_resource(
    "keda",
    "kedacore/keda",
    namespace="keda",
    flags=["--create-namespace"],
    resource_deps=["kedacore"],
)

# ScaledJobs: each spawns one worker Job per queued Redis item. Split per type
# so cpu/gpu can be read, deployed, and disabled independently. Both depend on
# KEDA (for the CRDs) + redis (the trigger source).
# ScaledJobs: each spawns one worker Job per queued Redis item. Split per type
# so cpu/gpu can be read, deployed, and disabled independently. Both depend on
# KEDA (for the CRDs) + redis (the trigger source).
k8s_yaml("./k8s/keda-cpu.yaml")
k8s_resource(
    "worker-cpu", # Target the auto-created resource directly
    resource_deps=["keda", "redis"],
)

k8s_yaml("./k8s/keda-gpu.yaml")
k8s_resource(
    "worker-gpu", # Target the auto-created resource directly
    resource_deps=["keda", "redis"],
)
