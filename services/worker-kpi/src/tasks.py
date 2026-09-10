"""
tasks.py — RQ task for post-simulation KPI computation.

Job envelope: {"public_id": "<uuid>"}. Reads the already-solved solution from
Postgres, decodes the HFR1 container (hfr1.py), computes KPIs (kpis.py), and
writes the results. Never touches ANUGA and never re-solves anything — that's
the reason this is its own worker/queue: redefining a KPI only means bumping
kpis.KPI_VERSION and re-running this task.
"""

import gzip

import db
import hfr1
import kpis


def compute_kpis(payload: dict):
    public_id = payload["public_id"]
    db.upsert_kpi_status(public_id, kpis.KPI_VERSION, "running")
    try:
        gz, sim_id = db.get_instance_solution_gz(public_id)
        if sim_id is None:
            raise LookupError(f"no simulation with public_id {public_id}")
        if gz is None:
            raise ValueError(f"instance {public_id} has no solution yet")

        dataset = hfr1.decode_result(gzip.decompress(gz))
        results = kpis.compute(dataset)

        db.save_kpi_results(public_id, kpis.KPI_VERSION, results)
        print(f"KPI worker: computed KPIs for {public_id} (version {kpis.KPI_VERSION})")
        return {"public_id": public_id, "kpi_version": kpis.KPI_VERSION}
    except Exception as e:
        db.upsert_kpi_status(public_id, kpis.KPI_VERSION, "error", str(e))
        raise
