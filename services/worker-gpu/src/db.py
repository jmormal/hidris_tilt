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
                    -- Legacy inline storage. Kept for rows written before the
                    -- large-object switch; new solutions go to solution_oid.
                    solution     BYTEA,
                    -- Large object OID, not inline BYTEA: psycopg2 sends bytea
                    -- parameters as hex-escaped SQL text, which doubles their
                    -- size, so a ~500MB gzipped solution blows past Postgres's
                    -- 1GB single-allocation ceiling ("invalid memory alloc
                    -- request size"). Large objects are streamed instead.
                    solution_oid OID
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_simulations_user_id "
                "ON simulations (user_id);"
            )
            # Migration for tables created before the large-object switch.
            cur.execute("ALTER TABLE simulations ADD COLUMN IF NOT EXISTS solution_oid OID;")
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


def get_instance_by_public_id(public_id: str):
    """Worker-side read, not scoped to a user.

    Every API-facing query filters on (public_id, user_id) so cross-user access
    is impossible by construction. A worker has no user context — it acts on
    behalf of whoever owns the row, exactly like save_solution_bytes — so it
    looks the instance up by public_id alone. Never expose this through a route.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT public_id, instance_name, instance_description,
                       is_solved, created_at, updated_at, instance
                FROM simulations
                WHERE public_id = %s;
                """,
                (public_id,),
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
                    solution_oid = NULL,
                    updated_at = now()
                WHERE public_id = %s AND user_id = %s
                RETURNING public_id, instance_name, instance_description,
                          is_solved, created_at, updated_at,
                          (SELECT solution_oid FROM simulations s
                           WHERE s.public_id = %s) AS old_solution_oid;
                """,
                (
                    name,
                    description,
                    Json(instance) if instance is not None else None,
                    public_id,
                    user_id,
                    public_id,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                # RETURNING subqueries read the pre-UPDATE snapshot, so this is
                # the object we just detached — unlink it or it leaks.
                old_oid = row.pop("old_solution_oid")
                if old_oid is not None:
                    _unlink_lobject(cur, old_oid)
        conn.commit()
        return row


def delete_instance(user_id: str, public_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Large objects are not owned by the row — dropping it would leak
            # the storage, so unlink explicitly in the same transaction.
            cur.execute(
                "DELETE FROM simulations WHERE public_id = %s AND user_id = %s "
                "RETURNING solution_oid;",
                (public_id, user_id),
            )
            row = cur.fetchone()
            deleted = row is not None
            if deleted and row[0] is not None:
                _unlink_lobject(cur, row[0])
        conn.commit()
        return deleted


def _unlink_lobject(cur, oid: int) -> None:
    """Drop a large object, tolerating one that's already gone."""
    try:
        cur.execute("SELECT lo_unlink(%s);", (oid,))
    except psycopg2.Error as e:
        print(f"DB: could not unlink large object {oid}: {e}")


SOLUTION_CHUNK = 8 << 20  # 8 MiB — bounds peak memory when streaming a result


def get_solution_bytes(user_id: str, public_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT solution, solution_oid, is_solved
                FROM simulations
                WHERE public_id = %s AND user_id = %s;
                """,
                (public_id, user_id),
            )
            row = cur.fetchone()
            if row is None:
                return None, None
            solution, oid, is_solved = row
            if oid is not None:
                lo = conn.lobject(oid, "rb")
                try:
                    return lo.read(), is_solved
                finally:
                    lo.close()
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

    Written as a large object streamed in chunks: as an inline bytea parameter
    psycopg2 hex-escapes the blob into the SQL text, doubling it, and anything
    past ~500MB makes the server reject the statement with "invalid memory
    alloc request size". Any previous solution's large object is unlinked so
    re-running a simulation doesn't leak its storage.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            lo = conn.lobject(0, "wb")
            try:
                for off in range(0, len(gz), SOLUTION_CHUNK):
                    lo.write(gz[off:off + SOLUTION_CHUNK])
                oid = lo.oid
            finally:
                lo.close()

            cur.execute(
                "SELECT solution_oid FROM simulations "
                "WHERE public_id = %s FOR UPDATE;",
                (public_id,),
            )
            row = cur.fetchone()
            if row is None:
                # No such instance — don't strand the object we just wrote.
                _unlink_lobject(cur, oid)
                conn.commit()
                raise LookupError(f"no simulation with public_id {public_id}")

            cur.execute(
                """
                UPDATE simulations
                SET solution = NULL, solution_oid = %s,
                    is_solved = TRUE, updated_at = now()
                WHERE public_id = %s;
                """,
                (oid, public_id),
            )
            if row[0] is not None and row[0] != oid:
                _unlink_lobject(cur, row[0])
        conn.commit()


# `data_grid` (the old inline bytea cube) only exists on tables created before
# the large-object switch. init_storms() does NOT create it on a fresh
# database — it only relaxes its NOT NULL when a legacy table already has it —
# so naming it in a SELECT is an UndefinedColumn error on any new deployment.
# Probe once per process and cache.
_HAS_LEGACY_DATA_GRID = None


def _has_legacy_data_grid(cur) -> bool:
    global _HAS_LEGACY_DATA_GRID
    if _HAS_LEGACY_DATA_GRID is None:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'storms' AND column_name = 'data_grid'
            ) AS present;
            """
        )
        row = cur.fetchone()
        _HAS_LEGACY_DATA_GRID = bool(
            row["present"] if isinstance(row, dict) else row[0]
        )
    return _HAS_LEGACY_DATA_GRID


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
            cols = (
                "timestep_s, n_frames, grid_rows, grid_cols, "
                "cell_size_m, units, nodata, data_grid_oid"
            )
            if _has_legacy_data_grid(cur):
                cols += ", data_grid"
            cur.execute(
                f"SELECT {cols} FROM storms WHERE public_id = %s;",
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
