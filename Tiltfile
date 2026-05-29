# ── Context guard ─────────────────────────────────────────────────────────────
# Tilt refuses to run against any context other than k3d-dev.
# If you named your cluster differently, update this string.
allow_k8s_contexts("k3d-hidris")

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
