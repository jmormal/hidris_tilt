---
name: hpc-image
description: >
  Change what runs on the vrhpc cluster — the anuga-gpu Singularity image, the
  bind-mounted source overrides that currently supersede it, and the credentials the
  job needs. Use when editing worker-gpu solver code that must take effect on the HPC,
  when rebuilding or shipping the SIF, when choosing CPU/GPU architecture flags, or
  when a password or auth key has to reach the cluster.
  Triggers: rebuild the SIF, ship to the cluster, update tasks.py on HPC, singularity
  image, gpu_arch, CPU_ARCH, zen3, pgpass, TS_AUTHKEY, ship secrets.
---

# Changing what runs on the cluster

## The running SIF is not what the Dockerfile produces

`~/containers/anuga-gpu.sif` is 16 GB, takes ~40 min to build and ~7 min to
upload, so **six** pieces are bind-mounted over it instead of rebuilt in. This
is the single most important fact about this path — a rebuild that does not fold
them in will look like it works and then fail exactly as before.

| Override in `~/containers/` | Supersedes | Why |
|---|---|---|
| `anuga-zen3/extracted/anuga` | the image's ANUGA | **the image copy is broken**: AVX-512 and cc86-only |
| `src-override/tasks.py` | `/app/src/tasks.py` | rank-0 build, storm broadcast + cache |
| `src-override/hpc_run.py` | `/app/src/hpc_run.py` | MPI-aware ranks, `MPI.Abort` on failure |
| `src-override/db.py` | `/app/src/db.py` | lazy pool (ranks without a tailnet must still import) |
| `src-override/db_proxy.py` | `/app/src/db_proxy.py` | proxied tailnet ops for multi-node |
| `hpc-entrypoint.sh`, `netns-run-worker.sh`, `mn-entrypoint.sh` | `/opt/hpc/*` | `--reset`, key redaction, mpirun-as-root |

Binds are **per file**, never per directory: `/app/src` also holds the 13 GB
DEM, and binding the directory over it would hide it.

## Shipping a solver change (the normal case)

A change to `tasks.py` or `hpc_run.py` costs a `scp`, not a rebuild:

```bash
scp -i ~/.ssh/hidris_hpc services/worker-gpu/src/{tasks,hpc_run,db,db_proxy}.py \
    jmormal@upvnet.upv.es@vrhpcadm1.dsic.upv.es:containers/src-override/
```

**Syntax-check before shipping, and check the rank guards.** A `NameError` on a
compute node costs a whole allocation, because the failed rank leaves its peers
deadlocked in a collective. Two checks that have each already caught a real bug:

```bash
python -m py_compile services/worker-gpu/src/*.py
# and: names assigned only under `if _is_root:` but read unconditionally
#      (this is how the elevation_file bug was caught before it shipped)
```

`_fetch_storm_cube` needs its own function-local `import db` — the module is
imported lazily elsewhere and the rank-0 path alone does not cover it.

## Rebuilding the SIF

```bash
nix-shell -p singularity squashfsTools
cd services/worker-gpu && ./build-sif.sh     # native mode only, never --oci
```

Three build arguments are load-bearing, not tuning:

- **`CPU_ARCH=zen3`** — nvc defaults to `-tp=host`. Built on a Zen 5 laptop it
  emits AVX-512; the EPYC 7453 nodes have none, and `import anuga` dies with
  SIGILL (exit 132, no traceback — numpy and scipy import fine, being generic
  wheels, which makes it look like an ANUGA bug).
- **`GPU_ARCH=cc80,cc86,cc89`** — A30 + A40 + Ada. Slurm advertises a bare
  `gpu:8` with no type, so a job cannot request one card over another. Missing
  an arch survives import and dies at the first kernel launch.
- **`NVHPC_TAG=...cuda_multi...`** — `docker build` has no GPU, so nvc reports
  "driver version (0)" and falls back to the oldest bundled toolkit (11.8). A
  single-CUDA base has no 11.8 to fall back to and the build dies outright.

After a rebuild that folds the overrides in, **delete the override binds from
`run-simulation.slurm` and `run-simulation-mn.slurm` together**, and drop the
warning block at the top of `DockerfileSingularity`.

## Shipping secrets

```bash
./services/worker-gpu/ship-hpc-secrets.sh    # verifies byte-for-byte afterwards
```

Two secrets, two mechanisms, for one reason:

- **The DB password goes in a `pgpass` file, never in env.** Singularity 3.7.3
  shell-evaluates every `--env` / `--env-file` / `SINGULARITYENV_` value before
  injecting it. A password containing `$` is silently truncated at the `$`; one
  containing `"` aborts the container with *reached EOF without closing quote*.
  There is no literal-passing mode in 3.7.3. libpq reads pgpass itself with no
  shell in the path, and `db.py` then connects with `password=None`.
- **`TS_AUTHKEY` may live in `worker.env`** only because `tskey-` values are
  `[A-Za-z0-9-]` and survive the shell pass unchanged.

The password travels on **ssh's stdin**, never as an argument, so it stays out
of `ps` and shell history. That is also the subtle trap: the remote script
therefore **cannot** be fed by heredoc, because a heredoc *is* stdin — the
remote `bash -s` consumes the script and the password never arrives. It is
base64'd into the command line instead, leaving stdin free. A wrong password
shipped this way is not obvious; it arrives as a plausible-looking file of the
wrong length. **Verify the byte count, and test the plumbing with a throwaway
value containing `$ " \ : space` before shipping the real one.**

Never let an auth key reach a Slurm `.out` file: `$HOME` is shared on the
cluster, `tailscale up` echoes its full command line on failure, and the scripts
`chmod 600` the log and `sed`-redact `tskey-*` for exactly that reason.
