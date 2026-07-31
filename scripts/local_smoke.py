"""Exercise the built Python SDK against the isolated local Docker API."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx

from kanopy import Kanopy, KanopyError

base_url = os.environ.get("SMOKE_BASE_URL", "http://localhost:18000/api/v1")
api_key = os.environ["SMOKE_API_KEY"]


def assert_invalid_key_is_rejected() -> None:
    try:
        with Kanopy("invalid-local-key", base_url=base_url) as client:
            client.get_identity()
    except KanopyError as exc:
        if exc.status_code != 401:
            raise AssertionError(
                f"invalid API key returned HTTP {exc.status_code}, expected 401"
            ) from exc
    else:
        raise AssertionError("invalid API key was accepted")


assert_invalid_key_is_rejected()

# Prove that block mode adds a second boundary after successful authentication.
# Rerun is an internal operator route and is intentionally absent from the
# customer integration contract.
blocked = httpx.post(
    f"{base_url}/jobs/{uuid4()}/rerun",
    headers={"Authorization": f"Bearer {api_key}"},
)
if blocked.status_code != 403:
    raise AssertionError(
        f"out-of-contract API-key call returned HTTP {blocked.status_code}, expected 403"
    )

project_id: str | None = None
job_id: str | None = None
with Kanopy(api_key, base_url=base_url) as client:
    identity = client.get_identity()
    organization = client.get_my_organization()
    if identity.get("email") != "sdk-smoke@kanopy.local":
        raise AssertionError("API key resolved to the wrong identity")

    project = client.create_project(
        name="Local SDK smoke test",
        description="Disposable project created by the Python SDK smoke test",
    )
    project_id = str(project["id"])
    if client.get_project(project_id)["id"] != project_id:
        raise AssertionError("created project could not be retrieved")

    try:
        with tempfile.TemporaryDirectory(prefix="kanopy-sdk-smoke-") as temp_dir:
            video = Path(temp_dir) / "multipart-smoke.mp4"
            # Slightly over 5 MiB forces two S3 parts at the minimum legal size.
            video.write_bytes(b"K" * (5 * 1024 * 1024 + 17))
            progress: list[tuple[int, int]] = []
            uploaded = client.upload_large(
                video,
                project_id=project_id,
                title="Local multipart smoke",
                upload_request_id="local-sdk-smoke-multipart",
                part_size=5 * 1024 * 1024,
                max_workers=2,
                part_retries=2,
                progress=lambda sent, total: progress.append((sent, total)),
            )
            job_id = str(uploaded["job_id"])
            if progress[-1] != (video.stat().st_size, video.stat().st_size):
                raise AssertionError(
                    "multipart progress did not reach the full file size"
                )
            if client.get_job(job_id)["id"] != job_id:
                raise AssertionError("uploaded job could not be retrieved")
    finally:
        if job_id is not None:
            client.delete_job(job_id)
        if project_id is not None:
            client.delete_project(project_id)

print(
    "PASS: authentication, blocked API-key contract, project CRUD, "
    f"and two-part MinIO upload ({organization.get('name', 'organization')})"
)
