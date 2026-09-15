---
name: hpc-triage
description: >
  Diagnose a Hidris HPC job that failed, died silently, produced no progress, or is
  stuck in the Slurm queue on vrhpc. Use when a simulation on the cluster errors or
  exits non-zero, when SSE goes quiet, when a job sits PENDING behind other users, or
  when asked why a run did not start or land on a node. Triggers: job failed, exit
  132, SIGILL, PMIX error, MPI abort, numprocs=1, stuck pending, priority, fairshare,
  why is my job queued, no progress, cluster status, free GPUs.
---

# Triaging an HPC job

## Read the first error, not the loudest one

An MPI job that aborts prints a wall of `PMIX ERROR: ... UNREACHABLE` and
`ORTE has lost communication` **after** one rank has already died. That torrent
is the aftermath of `MPI_Abort`, never the cause. Scroll up to the **first
Python traceback** in the `.out` file and diagnose that.

This bit me in reverse too: PMIX noise in a *single-node* job is not evidence of
a multi-node problem. Check `#SBATCH --nodes` / `ranks=` in the header before
reasoning about the fabric.

```bash
# on the cluster, in ~/containers
grep -n -m1 -A30 'Traceback' hidris-sim-<jobid>.out
sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,MaxRSS,NodeList
```

## Failure signatures seen on this cluster

| Symptom | Cause | Fix |
|---|---|---|
| exit 132, `Illegal instruction`, no traceback | image ANUGA built AVX-512 on a Zen 5 laptop; nodes are Zen 3 EPYC | the `anuga-zen3` bind override is missing — see `hpc-image` |
| `Failed to find device function` / `CUDA_ERROR_INVALID_IMAGE` | landed on an A30 (cc80) with a cc86-only build | build `cc80,cc86,cc89`; Slurm advertises bare `gpu:8` so you cannot request a card type |
| exit 3 after exactly 30 min, ~512 KB RSS, nothing logged | `flock -w 1800` on the per-node tailscale state dir | another sim holds the node; see [[hpc-slurm-job-serialisation]] |
| `numprocs=1` twice, one GPU idle, **no error** | `--cleanenv` stripped the `PMIX_*` vars srun exports, or srun used where mpirun belongs | multi-node: no `--cleanenv`. Single-node: `mpirun` *inside* the container |
| `MPI_ERR_INTERN` in `PyMPI_bcast` | proxychains `LD_PRELOAD` on connect() breaks MPI, even with `localnet` bypasses | never run the solver under proxychains; use `src/db_proxy.py` subprocesses |
| a collective (bcast/Gatherv) hangs forever across nodes | every node has `docker0` at the same `172.17.0.1/16`, so OpenMPI thinks they share a subnet | `OMPI_MCA_btl_tcp_if_include=eno2` |
| env var silently truncated, or container aborts with `reached EOF without closing quote` | Singularity 3.7.3 shell-evaluates every `--env`/`--env-file`/`SINGULARITYENV_` value | secrets with `$` or `"` go in a **pgpass file**, never in env |
| `mkdir /<path>: read-only file system` | an env var points into the read-only squashfs with no matching `-B` | add the bind, or point the var at `/scratch` |
| whole job wedges instead of failing | one rank raised while peers waited in a collective `MPI_Finalize` | `_abort_peers()` → `MPI.COMM_WORLD.Abort(1)` on any rank-local failure |

**The meta-lesson worth more than the table:** when a bug reproduces only in the
real path and never under a synthetic probe, suspect the **environment file**
before the algorithm. The 40× timestep regression was `OMP_PROC_BIND` in
`worker.env`; every probe ran `singularity exec` directly and so never loaded it,
which "exonerated" the halo exchange, partitioning, boundaries and operators in
turn.

## When a job will not start

```bash
squeue -u $USER --start                     # estimated start, or "Resources"
scontrol show job <jobid> | grep -E 'Reason|Priority|TRES'
sprio -l | head                             # priority breakdown per job
sinfo -N -o '%N %T %C %m %G'                # per-node: state, cpus, mem, gres
```

The cluster runs `priority/multifactor` with **age and fairshare** components.
This account has **no Slurm accounting association**, so it gets no fairshare
share at all — observed priority 1907 against 17638 for a user with an
association queueing 23 jobs. Ageing is the only lever that accrues, so a job
submitted and left alone eventually wins; cancelling and resubmitting **resets
the age factor and makes it worse**.

To jump the queue, do not raise priority — find a node with idle resources and
ask for a shape that fits it. Read free GPUs and free memory per node from
`sinfo`/`scontrol show node`, then size `--gres` and `--mem` to what is actually
free. A 200 G request on a node with 180 G free waits indefinitely while a
64 G request starts at once. **Memory, not GPUs, is usually what blocks.**

## Blast radius back in the cluster

Cancelling a job mid-storm-fetch orphans a Postgres backend stuck in `lo_read`,
holding a lock on `storms`. Every API pod then hangs at
`Waiting for application startup` — the schema init cannot take its locks. This
looks like a broken API, not a cancelled HPC job.

```bash
kubectl exec -it hidris-db-1 -- psql -U postgres -d hidris -c \
  "SELECT pid, state, wait_event, query FROM pg_stat_activity WHERE state <> 'idle';"
# then pg_terminate_backend(<pid>) on the orphan
```

Mitigated on both sides (`tcp_keepalives_*` on the cluster, `lock_timeout=15s`
on the API pool), but an orphan predating those still needs terminating by hand.
