# Hidris — study docs

Long-form explanations of how this project works, written to be read and
learned from rather than skimmed. `CLAUDE.md` at the repo root is the short
agent-facing summary; these are the deep versions.

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The whole system: how a drawn setup becomes a job, becomes a solved mesh, becomes pixels. Auth, queues, workers, database, infra. |
| [frontend-react.md](frontend-react.md) | How the React app is structured, and what every React hook it uses actually does — taught through this codebase's own files. |
| [solution-binary-format.md](solution-binary-format.md) | The `HFR1` binary container that carries a simulation result: byte layout, why it exists, how it's written and read. |

Suggested reading order: **architecture → frontend-react → solution-binary-format**.
The architecture doc gives you the map; the other two zoom into the two halves
you're most likely to change.

## A note on drift

Some of `CLAUDE.md` describes an older design that has since been replaced —
notably an RLE-encoded JSON result decoded through an LRU cache, and a
`useFloodLayer` hook. Neither exists any more. These docs describe the code as
it is today, and call out the replaced designs where the reason for the change
is worth knowing.
