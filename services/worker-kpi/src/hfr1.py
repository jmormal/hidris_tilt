"""
Python reader for the HFR1 solution container — mirrors
services/frontend/src/utils/decodeResult.ts exactly (see
docs/solution-binary-format.md for the full spec). Kept here, not shared with
the frontend, because the wire format's contract is "any reader that follows
the header's offsets/dtypes works" — the header is self-describing on purpose.

Unlike the frontend reader, which returns zero-copy views for a browser that
never re-touches the underlying bytes for long, this one is a KPI batch job:
copying out of the gzip buffer is fine and simpler.
"""

import json
import struct

import numpy as np

MAGIC = b"HFR1"

_DTYPES = {
    "f8": np.float64,
    "f4": np.float32,
    "i4": np.int32,
}


class SolutionFormatError(ValueError):
    pass


def decode_result(buf: bytes) -> dict:
    if len(buf) < 8:
        raise SolutionFormatError("Solution blob is truncated")

    magic = buf[0:4]
    if magic != MAGIC:
        raise SolutionFormatError(
            f"Unrecognised solution format {magic!r} — expected {MAGIC!r}. "
            "Re-run the simulation to regenerate it in the current format."
        )

    (head_len,) = struct.unpack_from("<I", buf, 4)
    header = json.loads(buf[8 : 8 + head_len].decode("utf-8"))
    if header.get("version") != 1:
        raise SolutionFormatError(f"Unsupported solution version {header.get('version')}")

    base = 8 + head_len
    blocks = header["blocks"]

    def read_block(name: str):
        spec = blocks[name]
        dtype = _DTYPES[spec["dtype"]]
        offset = base + spec["offset"]
        count = spec["length"]
        if offset % dtype().itemsize != 0:
            raise SolutionFormatError(f'Solution block "{name}" is misaligned at {offset}')
        return np.frombuffer(buf, dtype=dtype, count=count, offset=offset)

    n_frames = header["nFrames"]
    n_triangles = header["nTriangles"]

    return {
        "n_vertices": header["nVertices"],
        "n_triangles": n_triangles,
        "n_frames": n_frames,
        "times": np.asarray(header["times"], dtype=np.float64),
        # (nVertices, 2) lon/lat pairs, interleaved in the wire format.
        "vertex_lonlat": read_block("vertexLonLat").reshape(-1, 2),
        # (nTriangles, 3) vertex ids.
        "tri_indices": read_block("triIndices").reshape(-1, 3),
        "elevation": read_block("elevation"),
        # (nTriangles, nFrames) — wire layout is triangle-major, i.e. already
        # in this shape once reshaped (see docs/solution-binary-format.md §4).
        "depth": read_block("depth").reshape(n_triangles, n_frames),
        "speed": read_block("speed").reshape(n_triangles, n_frames),
    }
