"""Database and Redis access for a rank that must not be proxied itself.

Under multi-node MPI, rank 0 needs the tailnet (Postgres, Redis) while every
rank needs clean MPI over the cluster fabric. Running the solver itself under
proxychains4 satisfies the first and breaks the second: LD_PRELOAD hooks
connect() and MPI dies with MPI_ERR_INTERN inside bcast, localnet bypass rules
notwithstanding.

So the solver process is never proxied. Instead it shells out to THIS module,
which is short-lived and expendable, and which the caller runs under
proxychains. One process per operation, and there are only a handful: read the
setup, fetch the storm cube, store the solution, publish an event.

Invoked as:  proxychains4 -f <conf> -q python /app/src/db_proxy.py <cmd> [args]
Results come back on stdout as JSON, or as a file the caller named.
"""

import base64
import json
import sys


def _die(msg: str) -> None:
    print(json.dumps({"error": msg}), flush=True)
    sys.exit(1)


def cmd_get_instance(public_id: str) -> None:
    import db

    row = db.get_instance_by_public_id(public_id)
    if row is None:
        print(json.dumps({"row": None}), flush=True)
        return
    # RealDictRow is dict-like but not JSON-serialisable wholesale; take the
    # two fields the caller actually uses.
    print(json.dumps({"row": {
        "instance": row.get("instance"),
        "instance_name": row.get("instance_name"),
    }}), flush=True)


def cmd_get_storm(storm_ref: str, out_path: str) -> None:
    import numpy as np
    import db

    cube, meta = db.get_storm_cube(storm_ref)
    if cube is None:
        print(json.dumps({"found": False}), flush=True)
        return
    np.savez(out_path, cube=cube, meta=np.array(dict(meta), dtype=object))
    print(json.dumps({"found": True, "path": out_path,
                      "bytes": int(cube.nbytes)}), flush=True)


def cmd_save_solution(public_id: str, gz_path: str) -> None:
    import db

    with open(gz_path, "rb") as fh:
        gz = fh.read()
    db.save_solution_bytes(public_id, gz)
    print(json.dumps({"stored": len(gz)}), flush=True)


def cmd_publish(redis_url: str, channel: str, payload_b64: str) -> None:
    from redis import Redis

    Redis.from_url(redis_url).publish(channel, base64.b64decode(payload_b64))
    print(json.dumps({"published": True}), flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        _die("usage: db_proxy.py <get-instance|get-storm|save-solution|publish> ...")
    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "get-instance":
            cmd_get_instance(*args)
        elif cmd == "get-storm":
            cmd_get_storm(*args)
        elif cmd == "save-solution":
            cmd_save_solution(*args)
        elif cmd == "publish":
            cmd_publish(*args)
        else:
            _die(f"unknown command {cmd!r}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc(file=sys.stderr)
        _die(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
