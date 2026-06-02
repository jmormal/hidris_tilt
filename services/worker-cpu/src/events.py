"""
Shared pub/sub conventions for streaming ANUGA job progress.

Both the worker (publisher) and the API (subscriber) import from here so the
channel name and payload shape never drift apart.

Event types pushed onto the channel:
    queued | meshing | progress | complete | error

Each message is a JSON object: {"event": <type>, "data": {...}}
"""

import json


def channel_for(job_id: str) -> str:
    return f"sim:events:{job_id}"


def encode(event: str, data: dict) -> str:
    return json.dumps({"event": event, "data": data}, default=str)


def decode(raw: str) -> dict:
    return json.loads(raw)
