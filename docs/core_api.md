# Rust Core API

The bootstrap Core server listens on `127.0.0.1:32145`.

## Endpoints

- `GET /health`
- `POST /jobs`
- `GET /jobs/{id}`
- `POST /jobs/{id}/cancel`
- `GET /jobs/{id}/events` (SSE)

Job states are `queued`, `running`, `completed`, `failed`, and `cancelled`.

## Bootstrap job executor

Issue #2 establishes the framework before analysis handlers exist. The current request accepts:

```json
{
  "kind": "framework-smoke",
  "steps": 5,
  "fail_at_step": null
}
```

`steps` and `fail_at_step` are framework-test controls and can be replaced by typed job payloads as concrete analysis operations are introduced.

## Persistence policy

The first implementation keeps active and recently completed jobs in memory. Song artifacts remain outside the Job object and are referenced by string identifiers. A durable JobStore can replace the in-memory map without changing the HTTP contract; persistence of Song Data itself is implemented separately by Issue #3.

The API exposes timestamps, progress, stage, error text, and artifact references so GUI and evaluator clients do not depend on internal executor details.
