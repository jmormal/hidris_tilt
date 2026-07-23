import os
import json
import gzip
import io
from contextlib import contextmanager

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool

_pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
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
        # Connection itself is dead — discard it instead of returning it to
        # the pool, otherwise every subsequent request keeps drawing the same
        # broken connection and fails identically.
        broken = True
        raise
    finally:
        _pool.putconn(conn, close=broken)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS simulations (
                    id           SERIAL PRIMARY KEY,
                    public_id    UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
                    user_id      UUID NOT NULL,
                    instance_name        VARCHAR(255),
                    instance_description TEXT,
                    created_at   TIMESTAMPTZ DEFAULT now(),
                    updated_at   TIMESTAMPTZ DEFAULT now(),
                    is_solved    BOOLEAN NOT NULL DEFAULT FALSE,
                    instance     JSONB,
                    solution     BYTEA
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_simulations_user_id "
                "ON simulations (user_id);"
            )
        conn.commit()


def list_instances(user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT public_id, instance_name, instance_description,
                       is_solved, created_at, updated_at
                FROM simulations
                WHERE user_id = %s
                ORDER BY updated_at DESC;
                """,
                (user_id,),
            )
            return cur.fetchall()


def create_instance(
    user_id: str, name: str, description: str | None, instance: dict | None = None
):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO simulations
                    (user_id, instance_name, instance_description, instance)
                VALUES (%s, %s, %s, %s)
                RETURNING public_id, instance_name, instance_description,
                          is_solved, created_at, updated_at;
                """,
                (
                    user_id,
                    name,
                    description,
                    Json(instance) if instance is not None else None,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def get_instance(user_id: str, public_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT public_id, instance_name, instance_description,
                       is_solved, created_at, updated_at, instance
                FROM simulations
                WHERE public_id = %s AND user_id = %s;
                """,
                (public_id, user_id),
            )
            return cur.fetchone()


def update_instance(
    user_id: str,
    public_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    instance: dict | None = None,
):
    """
    Any change to the setup invalidates the solution: clears the stored
    solution bytes and sets is_solved = false, atomically. Only provided
    fields change (COALESCE keeps the rest).
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE simulations
                SET instance_name = COALESCE(%s, instance_name),
                    instance_description = COALESCE(%s, instance_description),
                    instance = COALESCE(%s, instance),
                    is_solved = TRUE,
                    solution = NULL,
                    updated_at = now()
                WHERE public_id = %s AND user_id = %s
                RETURNING public_id, instance_name, instance_description,
                          is_solved, created_at, updated_at;
                """,
                (
                    name,
                    description,
                    Json(instance) if instance is not None else None,
                    public_id,
                    user_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def delete_instance(user_id: str, public_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM simulations WHERE public_id = %s AND user_id = %s;",
                (public_id, user_id),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def get_solution_bytes(user_id: str, public_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT solution, is_solved
                FROM simulations
                WHERE public_id = %s AND user_id = %s;
                """,
                (public_id, user_id),
            )
            row = cur.fetchone()
            if row is None:
                return None, None
            solution, is_solved = row
            return (bytes(solution) if solution is not None else None), is_solved


def save_solution(public_id: str, dataset: dict):
    """
    Worker-side. Gzips a result dict and stores it, flipping is_solved.
    Use this when you have a Python dict. If you already have gzipped bytes
    (the GPU worker does), call save_solution_bytes instead to avoid
    double-compressing.
    """
    raw = gzip.compress(json.dumps(dataset).encode("utf-8"))
    save_solution_bytes(public_id, raw)


def save_solution_bytes(public_id: str, gz: bytes):
    """
    Worker-side. Stores already-gzipped solution bytes and flips is_solved.
    Keyed by public_id only — the worker acts on behalf of the owner.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE simulations
                SET solution = %s, is_solved = TRUE, updated_at = now()
                WHERE public_id = %s;
                """,
                (psycopg2.Binary(gz), public_id),
            )
        conn.commit()


def get_storm_cube(public_id: str):
    """
    Read-only, worker-side. Returns (cube ndarray (T,H,W) float32, meta dict)
    or (None, None). Mirrors services/api/src/db.py's function of the same
    name — the cube lives in a Postgres large object (data_grid_oid), not an
    inline bytea column, so a big multi-frame storm doesn't blow past
    Postgres's 1GB single-value ceiling.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT timestep_s, n_frames, grid_rows, grid_cols,
                       cell_size_m, units, nodata, data_grid_oid, data_grid
                FROM storms
                WHERE public_id = %s;
            """,
                (public_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None, None
            oid = row.pop("data_grid_oid")
            legacy_blob = row.pop("data_grid", None)
            if oid is None:
                if legacy_blob is None:
                    return None, None
                raw = bytes(legacy_blob)
            else:
                lo = conn.lobject(oid, "rb")
                try:
                    raw = lo.read()
                finally:
                    lo.close()
            buf = io.BytesIO(gzip.decompress(raw))
            cube = np.load(buf)
            return cube, row


if __name__ == "__main__":
    init_db()
