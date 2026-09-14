"""
KPI RQ Worker — KEDA-compatible, spot-safe, same shape as
worker-cpu/worker-gpu's worker.py (kept as a parallel copy, not shared code,
per this repo's convention — see CLAUDE.md).

Environment variables:
  TETIS_REDIS_URL   redis url               (default: redis://redis:6379)
  QUEUE             queue(s) to drain, comma-separated (default: jobs:kpi)
  MAX_SPOT_RETRIES  re-queue attempts on SIGTERM (default: 3)
"""

import os
import signal
import uuid

from redis import Redis
from rq import Worker, Queue

WORKER_NAME = f"worker-kpi-{uuid.uuid4().hex[:8]}"
WORKER_QUEUES = os.getenv("QUEUE", "jobs:kpi").split(",")
REDIS_URL = os.getenv("TETIS_REDIS_URL", "redis://redis:6379")
MAX_SPOT_RETRIES = int(os.getenv("MAX_SPOT_RETRIES", "3"))


class SpotGracefulWorker(Worker):
    """Intercepts SIGTERM (node drain / spot reclaim / KEDA scale-down /
    rolling update) and re-queues the in-flight job instead of losing it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_job = None
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def perform_job(self, job, queue):
        self._current_job = job
        try:
            return super().perform_job(job, queue)
        finally:
            self._current_job = None

    def _handle_sigterm(self, signum, frame):
        job = self._current_job
        if job is not None:
            retries = job.meta.get("spot_retries", 0)
            if retries < MAX_SPOT_RETRIES:
                retries += 1
                print(f"[{WORKER_NAME}] SIGTERM — re-queuing job {job.id} "
                      f"(attempt {retries}/{MAX_SPOT_RETRIES})")
                job.meta["spot_retries"] = retries
                job.save_meta()
                job.requeue()
            else:
                print(f"[{WORKER_NAME}] SIGTERM — job {job.id} exceeded "
                      f"max retries ({MAX_SPOT_RETRIES}), marking failed.")
        else:
            print(f"[{WORKER_NAME}] SIGTERM — no active job, shutting down.")
        self.request_stop(signum, frame)


if __name__ == "__main__":
    import tasks  # noqa: F401 — registers compute_kpis for RQ's job lookup

    redis_conn = Redis.from_url(REDIS_URL)
    print(f"[{WORKER_NAME}] Starting, draining {WORKER_QUEUES} via {REDIS_URL}")
    queues = [Queue(name, connection=redis_conn) for name in WORKER_QUEUES]
    worker = SpotGracefulWorker(queues, connection=redis_conn, name=WORKER_NAME)
    # burst: drain what is queued, then exit. Without it, work() blocks forever
    # waiting for a job — and KEDA's ScaledJob is a run-once model, so a pod
    # that arrives to an empty queue never returns. That happens routinely:
    # pollingInterval is 15s and maxReplicaCount is 4, so KEDA can spawn several
    # pods for one queue item, or spawn one just as another worker takes the
    # last job. The loser then sits in work() until activeDeadlineSeconds (1500)
    # SIGKILLs it — six such pods accumulated over 23h, each holding a slot for
    # 25 minutes, with no logs and a DeadlineExceeded that says nothing about
    # the cause.
    worker.work(burst=True)
