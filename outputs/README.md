# outputs/

Bounded, sanitized R&D evidence only (`docs/14-phase0-implementation-execution-plan.md` §4, §20). Not a durable store. Never contains secrets, credentials, raw audio, or restricted user data.

## Evidence naming

```text
outputs/evidence/<wp>-<slug>/<YYYYMMDD-HHMMSS>-<artifact>.md
```

- `<wp>`: `wp00` … `wp12`
- `<slug>`: short kebab-case work-package name (for example `preflight`, `scaffold`, `mock-slice`)
- timestamp: local time of the run, 24-hour
- `<artifact>`: `report`, `gate`, `compat`, `latency`, `cost`, …

Only `*.md` reports under `outputs/evidence/` are tracked by Git. Traces, screenshots, videos, and spreadsheets stay local (ignored) and are referenced from the report by relative path when needed.

Each report records what `docs/14` §20 requires: application/config version, lock checksum when relevant, tests run with pass/fail counts, sanitized compatibility results, provider identity when used, measured latency/usage/cost, and known limitations.
