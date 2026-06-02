"""
TETIS RQ Worker — KEDA-compatible, multi-queue, spot-safe.

Handles SIGTERM (Karpenter / spot termination) by re-queuing
the in-flight job up to MAX_SPOT_RETRIES times before marking it failed.

Environment variables:
  REDIS_HOST          Redis hostname           (default: localhost)
  REDIS_PORT          Redis port               (default: 6379)
  WORKER_QUEUES       Comma-separated queues   (default: tetis-jobs)
  WORKER_NAME         Worker identifier        (default: auto from hostname)
  MAX_SPOT_RETRIES    Re-queue attempts on SIGTERM (default: 3)
"""

import os
import signal
import socket
import uuid
from redis import Redis
from rq import Worker, Queue
from tasks import run_anuga

# --- Cmnfiguration ---
WORKER_QUEUES = ["jobs:cpu"]
MAX_SPOT_RETRIES = 3

WORKER_NAME = f"worker-{uuid.uuid4().hex[:8]}"
WORKER_QUEUES = os.getenv("QUEUE", "jobs:gpu").split(",")
REDIS_URL = os.getenv("TETIS_REDIS_URL", "redis://redis:6379")


class SpotGracefulWorker(Worker):
    """
    Custom RQ Worker that intercepts SIGTERM and re-queues the
    in-flight job instead of letting it die silently.

    Kubernetes sends SIGTERM when:
      - Karpenter / Cluster Autoscaler drains a node
      - A spot instance is reclaimed
      - The Deployment is scaled down (KEDA cooldown)
      - A rolling update replaces the pod

    The job's meta dict tracks retry count so we don't loop forever.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_job = None
        self._current_queue = None
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def perform_job(self, job, queue):
        self._current_job = job
        self._current_queue = queue

        print(f"Starigng job {job} from queue {queue}")
        try:
            return super().perform_job(job, queue)
        finally:
            self._current_job = None
            self._current_queue = None

    def _handle_sigterm(self, signum, frame):
        if self._current_job:
            job = self._current_job
            retries = job.meta.get("spot_retries", 0)

            if retries < MAX_SPOT_RETRIES:
                retries += 1
                print(
                    f"[{WORKER_NAME}] SIGTERM — re-queuing job {job.id} "
                    f"(attempt {retries}/{MAX_SPOT_RETRIES})"
                )
                job.meta["spot_retries"] = retries
                job.meta["status_message"] = (
                    f"Re-queued due to spot termination (attempt {retries})"
                )
                job.save_meta()
                job.requeue()
            else:
                print(
                    f"[{WORKER_NAME}] SIGTERM — job {job.id} exceeded "
                    f"max retries ({MAX_SPOT_RETRIES}), marking failed."
                )
                job.meta["status_message"] = (
                    f"Failed: exceeded max spot retries ({MAX_SPOT_RETRIES})"
                )
                job.save_meta()
        else:
            print(f"[{WORKER_NAME}] SIGTERM — no active job, shutting down.")

        self.request_stop(signum, frame)


if __name__ == "__main__":
    import tasks  # noqa: F401

    REDIS_URL = os.getenv("REDIS_URL", "localhost")
    REDIS_QUEUE = os.getenv("REDIS_CPU", "jobs:gpu")
    WORKER_QUEUES = os.getenv("QUEUE", "jobs:gpu").split(",")
    REDIS_URL = os.getenv("TETIS_REDIS_URL", "redis://redis:6379")
    print(REDIS_URL)
    redis_conn = Redis.from_url(REDIS_URL)
    print(f"[{WORKER_NAME}] Starting spot-safe worker")

    queues = [Queue(name, connection=redis_conn) for name in WORKER_QUEUES]
    worker = SpotGracefulWorker(
        queues, connection=redis_conn, name=WORKER_NAME)
    worker.work()
