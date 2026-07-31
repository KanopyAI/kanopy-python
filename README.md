# Kanopy Python SDK

`kanopy-ai` is the Python client for Kanopy's infrastructure inspection API.
It covers the core integration workflow: create a project, upload footage,
monitor processing, query network inventory, and download results.

## License and service boundary

The client library in this repository is licensed under the Apache License,
Version 2.0. Copyright 2026 Kanopy AI, Inc.

The license applies to the SDK source code only. It does not grant access to
the Kanopy API or rights in Kanopy's hosted service, backend implementation,
models, processing systems, customer data, trade names, or trademarks. API
access requires credentials issued by Kanopy and remains subject to the
applicable customer agreement and service terms.

Applications that merely use the SDK through its interfaces remain separable
works under Apache-2.0. Utilities retain their independently developed
applications and data; Kanopy retains its SDK, platform, and pre-existing
intellectual property.

## Install

```bash
pip install kanopy-ai
```

## Quick start

```python
from kanopy import Kanopy

with Kanopy(api_key="kpy_live_...") as kanopy:
    project = kanopy.create_project(
        name="North corridor",
        description="Q3 inspection",
    )
    upload = kanopy.upload(
        "flight.mp4",
        metadata="flight.srt",
        project_id=project["id"],
        title="North corridor flight 01",
        upload_request_id="north-corridor-flight-01",
    )

    # Reconstruction is queued automatically when the upload completes.
    job = kanopy.wait_for_job(upload["job_id"], timeout=60 * 60)
    trees = kanopy.list_project_trees(project["id"], job_id=job["id"])
```

The default base URL is `https://app.kanopy-ai.com/api/v1`. For staging or
local development, pass `base_url=` when constructing `Kanopy`.

## Large video uploads

Use `upload_large` for production inspection footage. It creates an idempotent
multipart session, uploads parts directly to object storage, retries each
failed part with a fresh presigned URL, and queues reconstruction when all
parts are complete:

```python
def report_progress(sent: int, total: int) -> None:
    print(f"{sent / total:.0%}")

upload = kanopy.upload_large(
    "large-flight.mp4",
    metadata="flight.srt",
    project_id=project["id"],
    title="North corridor flight 02",
    upload_request_id="north-corridor-flight-02",
    capture_device="drone",
    line_clearance=True,
    progress=report_progress,
)
```

The default uses 64 MiB parts, four parallel workers, and three attempts per
part. `part_size`, `max_workers`, and `part_retries` are configurable. Memory
use is approximately `part_size * max_workers` while transfers are active.

## Pagination

List methods return a `Page`. Offset pagination is used by default. Pass
`cursor=""` to start keyset pagination, then use `page.next_cursor`:

```python
page = kanopy.list_jobs(cursor="", limit=100)
while True:
    for job in page.items:
        print(job["id"], job["status"])
    if not page.next_cursor:
        break
    page = kanopy.list_jobs(cursor=page.next_cursor, limit=100)
```

## Errors and request IDs

Non-successful API responses raise `KanopyError`. The exception exposes the
HTTP status, Kanopy error code, detail payload, and support request ID:

```python
from kanopy import KanopyError

try:
    kanopy.get_job("missing-id")
except KanopyError as exc:
    print(exc.status_code, exc.code, exc.request_id)
```

## Isolated local API smoke test

The smoke harness builds the current backend and runs the installed SDK against
a disposable Docker stack with Postgres, Redis, and MinIO. It uses real bearer
authentication, enables `API_KEY_CONTRACT_MODE=block`, performs a two-part
presigned upload, and removes all containers and volumes when it finishes.

```bash
./scripts/run_local_smoke.sh
```

The stack uses `localhost:18000` for the API and `localhost:19100` for MinIO so
it can run alongside the normal development Compose project.

The SDK keeps a reviewed copy of Kanopy's public OpenAPI schema under
`tests/fixtures/`. From the Kanopy development repository root, refresh that
copy after an intentional public API change with:

```bash
./scripts/sync_public_openapi.sh
```
