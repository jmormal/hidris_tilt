---
name: hpc-solve
description: >
  Submit an ANUGA flood simulation to the UPV vrhpc Slurm cluster and follow it to
  completion — choosing GPUs/RAM/nodes, predicting mesh size and runtime, and reading
  the progress output. Use when asked to run, start, submit, queue or re-queue a
  simulation on the HPC/cluster/slurm/server GPU, to pick resources for a run, to
  report how a running job is doing, or to estimate how big a mesh or result will be.
  Triggers: run it on the HPC, send it to the cluster, sbatch, submit the job,
  how is the run going, is it queued, multi-node, how many GPUs, mesh size.
---

# Running a solve on vrhpc

## Reaching the cluster

The cluster sits behind the **UPV VPN, which drops regularly**. A
`Connection timed out` on port 22 means the VPN, not the cluster — say so
rather than diagnosing the cluster, and fall back to data already in Postgres
where the question allows it.

```bash
ssh -i ~/.ssh/hidris_hpc -o IdentitiesOnly=yes \
    jmormal@upvnet.upv.es@vrhpcadm1.dsic.upv.es 'squeue -u $USER'
```

The doubled `@` is correct: the SSH *user* is `jmormal@upvnet.upv.es` and the
host is `vrhpcadm1.dsic.upv.es`. A bare `jmormal` authenticates as a different,
non-existent account and the password/key is rejected — which reads as a wrong
credential rather than a wrong username.

Everything lives in `~/containers/` on the login node: the SIF, `worker.env`,
`pgpass`, both `.slurm` scripts, the override tree, and the `hidris-sim-*.out`
logs.

## Two ways to submit

**Through the API** (normal path — the frontend's Run button, and what keeps
the SSE progress stream working):

```
POST /api/instances/{public_id}/simulate?target=hpc&gpus=N&mem_gb=M&nodes=K
```

`services/api/src/hpc.py` validates the numbers, builds the `--gres` /
`--cpus-per-task` / `--nodes` overrides and picks the script:
`nodes > 1` selects `run-simulation-mn.slurm`, otherwise
`run-simulation.slurm`. Caps: 8 GPUs, 480 GB, 6 nodes.

**Directly by sbatch**, when the API is down or you are testing the script:

```bash
cd ~/containers && PUBLIC_ID=<uuid> JOB_ID=<uuid> \
  sbatch --parsable --gres=gpu:4 --cpus-per-task=5 --mem=200G run-simulation.slurm
```

`PUBLIC_ID` and `JOB_ID` must both be exported — the script hard-fails without
them. `JOB_ID` is the channel the frontend streams on (`sim:events:$JOB_ID`);
submitting by hand with a fresh UUID means nobody is listening, which is fine
for a test but produces "no progress" reports that are not a bug.

**Before re-queueing a cancelled job, record its `PUBLIC_ID` from the old job's
environment or the API.** Guessing the instance from "most recently updated and
unsolved" is how a run gets pointed at the wrong instance; an instance with no
region polygon then dies at `tasks.py:849` (`features["region"][0]`,
IndexError) minutes in, and the mistake looks like a code bug.

## Choosing resources

| Knob | What it does | Sane default |
|---|---|---|
| `gpus` | one MPI rank per GPU, **per node** | 8 single-node, 4/node multi |
| `mem_gb` | sized for the *result*, not the solve | 200 G at 4.6M triangles |
| `nodes` | >1 switches to the srun/PMIx script | 1 unless proving scale |

RAM is the binding constraint, not GPUs. The solve itself stays near 10 GB even
at 1.8M triangles; `_finalize_result` peaks at roughly
`10 × frames × triangles × 4 bytes` because it stacks three per-frame arrays and
derives four more full-size `(T,N)` arrays before the wet filter drops anything.
Frames multiply harder than triangles — doubling `output_timestep` is a cheaper
saving than coarsening the mesh.

## Predicting mesh size

`region_area_m2 / mesh_max_area` gives the naive triangle count; ANUGA's quality
constraints push the real count to about **1.53×** that. Measured on two
independent runs (1196.4 km² at 400 m² → 4.58M; 579.5 km² likewise). Use it to
sanity-check a submission before burning an allocation.

Reference point: **4.6M triangles, 8 GPUs on one node, 29:49 wall, 485 MB
result** (job 148466). Multi-node has only ever run a ~4k-triangle instance —
the comparison worth making is 4.6M on 2 nodes × 4 GPUs against that 29:49.

## Following a run

Progress arrives on Redis pub/sub and reaches the frontend over SSE unmodified,
so the UI is the normal answer. When it is quiet, the Slurm log is the evidence:

```bash
tail -f ~/containers/hidris-sim-<jobid>.out       # on the cluster
GET /api/hpc/simulation/{slurm_job_id}/log        # or through the API
```

The header lines worth reading first: `ranks=N (gpus on node=N)` confirms the
GPU selector took effect, the `nvidia-smi` line shows which card you actually
landed on, and `Elements:` per rank is the real mesh size (sum across ranks,
then subtract ~0.7% for ghost duplication).

**A healthy timestep is ~1.1 ms.** If it is ~45 ms, that is
`OMP_PROC_BIND`/`OMP_PLACES` leaking into `worker.env` — see
[[hpc-multigpu-state]] in memory. Do not go looking at the halo exchange.

## Related

- Failures, stuck queues and misleading errors → the `hpc-triage` skill
- Rebuilding or shipping the SIF → the `hpc-image` skill
