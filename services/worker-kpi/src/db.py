"""
Postgres access for worker-kpi. Parallel copy of the relevant slice of
services/api/src/db.py (this repo's convention: worker DB modules are not
shared code with the API, see worker-gpu/src/db.py) — trimmed to only what a
KPI job needs: reading a solved instance's solution bytes, the OSM reference
tables, and writing kpis results.
"""

import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor, Json, execute_values
from psycopg2.pool import ThreadedConnectionPool

_pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=5,
    host=os.getenv("DB_HOST"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
    port=os.getenv("DB_PORT", "5432"),
)


@contextmanager
def get_conn():
    conn = _pool.getconn()
    broken = False
    try:
        yield conn
    except psycopg2.OperationalError:
        broken = True
        raise
    finally:
        _pool.putconn(conn, close=broken)


def get_instance_solution_gz(public_id: str):
    """Worker-side, no ownership check — mirrors save_solution_bytes's trust
    model (the worker acts on behalf of the owner, keyed by public_id alone).
    Returns (gz_bytes, simulation_id): gz_bytes is None if unsolved, and
    simulation_id is None only if no such instance exists at all."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, solution, solution_oid, is_solved FROM simulations "
                "WHERE public_id = %s;",
                (public_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None, None
        sim_id, solution, oid, is_solved = row
        if not is_solved:
            return None, sim_id
        if oid is not None:
            lo = conn.lobject(oid, "rb")
            try:
                return lo.read(), sim_id
            finally:
                lo.close()
        return (bytes(solution) if solution is not None else None), sim_id


def get_missing_osm_coverage(cells: list[tuple[int, int]], grid_deg: float = 0.05):
    if not cells:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cell_x, cell_y FROM osm_coverage WHERE grid_deg = %s;",
                (grid_deg,),
            )
            covered = {tuple(row) for row in cur.fetchall()}
    return [c for c in cells if c not in covered]


def mark_osm_coverage(cells: list[tuple[int, int]], grid_deg: float = 0.05):
    if not cells:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO osm_coverage (cell_x, cell_y, grid_deg) VALUES %s "
                "ON CONFLICT (cell_x, cell_y, grid_deg) DO NOTHING;",
                [(x, y, grid_deg) for x, y in cells],
            )
        conn.commit()


def upsert_osm_buildings(rows: list[dict]):
    """rows: [{"osm_id", "wkt", "building_type", "amenity", "tags"}]."""
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO osm_buildings (osm_id, geom, building_type, amenity, tags)
                VALUES %s
                ON CONFLICT (osm_id) DO NOTHING;
                """,
                [
                    (
                        r["osm_id"],
                        r["wkt"],
                        r.get("building_type"),
                        r.get("amenity"),
                        Json(r.get("tags") or {}),
                    )
                    for r in rows
                ],
                template="(%s, ST_Multi(ST_SetSRID(ST_GeomFromText(%s), 4326)), %s, %s, %s)",
            )
        conn.commit()


def upsert_osm_landuse(rows: list[dict]):
    """rows: [{"osm_id", "wkt", "landuse_class", "tags"}]."""
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO osm_landuse (osm_id, geom, landuse_class, tags)
                VALUES %s
                ON CONFLICT (osm_id) DO NOTHING;
                """,
                [
                    (r["osm_id"], r["wkt"], r["landuse_class"], Json(r.get("tags") or {}))
                    for r in rows
                ],
                template="(%s, ST_Multi(ST_SetSRID(ST_GeomFromText(%s), 4326)), %s, %s)",
            )
        conn.commit()


def get_osm_buildings_in_bbox(min_lon, min_lat, max_lon, max_lat):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT osm_id, ST_AsGeoJSON(geom) AS geojson,
                       building_type, amenity, tags
                FROM osm_buildings
                WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);
                """,
                (min_lon, min_lat, max_lon, max_lat),
            )
            return cur.fetchall()


def get_osm_landuse_in_bbox(min_lon, min_lat, max_lon, max_lat):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT osm_id, ST_AsGeoJSON(geom) AS geojson, landuse_class, tags
                FROM osm_landuse
                WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);
                """,
                (min_lon, min_lat, max_lon, max_lat),
            )
            return cur.fetchall()


def upsert_kpi_status(public_id: str, kpi_version: int, status: str, error_message=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kpis (simulation_id, kpi_version, status, error_message)
                SELECT id, %s, %s, %s FROM simulations WHERE public_id = %s
                ON CONFLICT (simulation_id, kpi_version)
                DO UPDATE SET status = EXCLUDED.status,
                              error_message = EXCLUDED.error_message,
                              computed_at = now();
                """,
                (kpi_version, status, error_message, public_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise LookupError(f"no simulation with public_id {public_id}")
        conn.commit()


def save_kpi_results(public_id: str, kpi_version: int, results: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kpis (simulation_id, kpi_version, status, results, computed_at)
                SELECT id, %s, 'complete', %s, now() FROM simulations WHERE public_id = %s
                ON CONFLICT (simulation_id, kpi_version)
                DO UPDATE SET status = 'complete', results = EXCLUDED.results,
                              error_message = NULL, computed_at = now();
                """,
                (kpi_version, Json(results), public_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise LookupError(f"no simulation with public_id {public_id}")
        conn.commit()
