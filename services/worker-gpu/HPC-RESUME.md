# HPC worker: state of play

Last verified 2026-09-10 against `vrhpcadm1.dsic.upv.es` (Slurm partition
`batch`, 6 × A40 nodes).

## Working

| what | evidence |
|---|---|
| SSH from the laptop and from the API pod | key auth as `jmormal@upvnet.upv.es`; the api pod resolves the host and gets an SSH banner |
| `anuga-gpu.sif` on the cluster | 16GB, shipped to `~/containers/`, `singularity sif list` shows a classic Squashfs partition (3.7.3 cannot read OCI-SIF) |
| SIF runs on the cluster | `singularity exec` → python 3.10.12, numpy 2.2.6 |
| GPU match | nodes are A40 = **cc86**, and the image was built `GPU_ARCH=cc86,cc89` |
| user namespaces on compute nodes | `unshare --user --map-root-user --net --mount` → OK; `slirp4netns` present |
| the tailnet transport | a job on vrhpc2 brought up netns tailscale, resolved `hidris-db-1` over MagicDNS and **reached Postgres** (it got a password error from the server, i.e. TCP+TLS+auth round trip completed) |

## The trap that cost the most: SIGILL from the CPU target

`import anuga` died with `Illegal instruction (core dumped)`, exit 132, and no
Python traceback. `numpy` and `scipy` imported fine — they are generic pip
wheels with runtime dispatch — which makes it look like an ANUGA bug rather
than a portability one.

| | CPU | AVX-512 |
|---|---|---|
| the laptop that built the SIF | Ryzen 7 260 (Zen 5) | **yes** |
| vrhpc2 / vrhpc4 | EPYC 7453 (Zen 3) | **no** |

`nvc` defaults to `-tp=host`: the machine doing the build. So ANUGA was
compiled with AVX-512 the compute nodes cannot execute. **The HPC path could
never have worked with that image**, which is why the note about the SIF
running fine locally was not evidence of anything about the cluster.

Two fixes, both in the tree:

- `DockerfileSingularity` now takes `CPU_ARCH` (default `zen3`) and passes
  `-tp=` through `CFLAGS`/`CXXFLAGS`. This is the real fix, and it needs a full
  image + SIF rebuild to take effect.
- `build-anuga.slurm` (submitted to the cluster) recompiles **only** the 24MB
  anuga package on a compute node, where nvc's host default is the target by
  construction, and `run-simulation.slurm` binds the result over the image's
  copy. This is what makes it work today without a 40-minute rebuild, a
  30-60 minute SIF build and a 16GB upload.

Two snags in that build worth remembering: meson-python writes its build tree
into the source dir, which is read-only squashfs inside the SIF (copy the
source to a writable bind first), and `pip install --prefix` still routes
console scripts to `/usr/local/bin` because Ubuntu patches Python with a
`posix_local` scheme — so build a **wheel** and unzip it instead of installing.

If the cluster ever turns out to be heterogeneous, rebuild with `-tp=px`
(generic x86-64) rather than per-node packages.

## The cluster is heterogeneous in GPU — build for every arch

vrhpc1 and vrhpc2 carry **A40s (cc86)**; vrhpc4 carries an **A30 (cc80)**.
vrhpc3/5/6 are unverified — `nvidia-smi` in a job without `--gres` reports "No
devices were found", because Slurm masks the GPUs from a job that did not ask
for one, so the only way to inventory them is to hold a GPU on each node.

Slurm advertises a bare `gpu:8` with no type, so **a job cannot request one card
over the other** — which node you land on is a lottery. A binary missing the
arch of whichever card it gets imports fine and then dies at the first kernel
launch:

```
Accelerator Fatal Error: Failed to find device function 'nvkernel_..._F1L1413_2'!
File was compiled without -gpu
Rebuild this file with -gpu=cc80 to use NVIDIA Tesla GPU 0
```

Note how misleading that is. "File was compiled without -gpu" suggests a build
misconfiguration; the kernel was in fact present (123 symbols, sm_86 cubins) and
the real message is the second line — wrong *architecture*, not missing code.
Worse, it is intermittent: the same image works on vrhpc1/2 and fails on vrhpc4.

Both the SIF Dockerfile and `build-anuga.slurm` now build `cc80,cc86,cc89`
(yielding sm_70/75/80/86/89/90 in the fatbin). Do not narrow this to the arch of
whatever node you happened to test on.

`gputest.py` on the cluster is a ~1 minute reproducer that reaches a real kernel
launch — far faster than a full simulation for this class of bug, and it is what
surfaced the `-gpu=cc80` line that the truncated Slurm log had cut off.

## The two traps that cost the most time

**Singularity 3.7.3 shell-evaluates every environment value.** `--env`,
`--env-file` and `SINGULARITYENV_*` all end up in a sourced script, so:

- a value containing `$` is truncated at the `$` (`ab$cd` → `ab`), *even in
  single quotes*;
- a value containing `"` kills the container: `reached EOF without closing quote`;
- `SINGULARITY_NO_EVAL=1 --env` doesn't help — the flag parser rejects the `"`
  before evaluation.

So **no password can be carried in worker.env**. The DB password goes in a
libpq `pgpass` file instead, bind-mounted to `/pgpass` with `PGPASSFILE=/pgpass`;
libpq parses it itself with no shell involved. `db.py` then connects with
`password=None` and libpq fills it in. `TS_AUTHKEY` may stay in worker.env only
because `tskey-…` values are `[A-Za-z0-9-]`.

Use `./ship-hpc-secrets.sh` — it hashes the password on both ends and refuses to
report success unless they match. A silently mangled password looks exactly like
a wrong password in the Postgres log, so verify rather than assume.

**The two transports share a tailscale state dir and disagree about
`--accept-dns`.** `hpc-entrypoint.sh`'s proxychains fallback brings tailscale up
with `--accept-dns=false`; `netns-run.sh` does not, because it writes
`resolv.conf` itself. Whichever ran first made the other refuse to start:
`changing settings via 'tailscale up' requires mentioning all`. Both now pass
`--reset`. One fallback run used to poison every later netns run on that node.

## Layout on the cluster (`~/containers/`)

```
anuga-gpu.sif            the image (16GB)
run-simulation.slurm     submitted by the API; binds and env live here
worker.env               non-secret env + TS_AUTHKEY.  NO PG_PASSWORD.
pgpass                   *:*:*:hidris:<password>, mode 600
hpc-entrypoint.sh        override, bind-mounted over the SIF's copy
netns-run-worker.sh      override, bind-mounted over the SIF's copy
hidris-sim-<jobid>.out   Slurm log, mode 600
```

The two `*-override* ` scripts exist so a one-line fix to the tailnet path does
not cost a 40-minute rebuild plus a 16GB upload. `run-simulation.slurm` binds
them over `/opt/hpc/{entrypoint,netns-run}.sh` when present, and works without
them. **Fold them back in at the next SIF rebuild** — the copies in the image are
stale the moment either is edited.

Slurm `.out` files are chmod 600 by the job: `$HOME` is shared, and `tailscale up`
echoes its whole command line (auth key included) when it fails.

## Control path

`POST /api/instances/{id}/simulate?target=slurm` → `src/hpc.py` → `sbatch`.
`target=hpc` is a *different* thing: it enqueues on `jobs:hpc` for a SIF worker
to pull, needing no scheduler access. The UI's third toggle button is `slurm`.

The API reaches the login node by **ordinary DNS**, not the tailscale egress
proxy: `vrhpcadm1`'s own tailscaled has been offline since 2026-08, so the
ExternalName in `k8s/api.yaml` points at a dead device. It is left in place for
when the node rejoins. Auth is key → password → keyless Tailscale SSH, first one
configured wins (`src/remote.py`).

## Known-stale, still to do

- **Tailnet name drift.** The operator wants `hidris-db` / `hidris-redis` but
  stale *offline* devices hold those names, so the live proxies registered as
  `hidris-db-1` / `hidris-redis-1`, which is what worker.env points at. Every
  cluster rebuild bumps the suffix again (`tailscale-operator-1/-2/-3` already
  exist). Deleting the dead devices in the admin console is the actual fix.
- The KPI job is enqueued by `run_anuga`, which the HPC path does not call — it
  goes straight to `_run_gpu_worker`. HPC solves therefore leave KPIs stale.
- `_run_gpu_worker` leaves `elevation_file` unbound if a setup has no `region`
  feature (`tasks.py:637`); it is a `NameError`, not a clear message. Affects
  the in-cluster path identically.
- The storm-cube concern in the notes still stands: `db.get_storm_cube()` pulls
  the whole 666MB raster to use a few km of it. Instances without a storm avoid
  it entirely — use one of those to test.
