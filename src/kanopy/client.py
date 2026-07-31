"""Synchronous client for the Kanopy Developer API."""

from __future__ import annotations

import time
import json
import math
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
from os import PathLike
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence

import httpx

from .errors import KanopyError, KanopyUploadError
from .models import Page

DEFAULT_BASE_URL = "https://app.kanopy-ai.com/api/v1"
DEFAULT_MULTIPART_PART_SIZE = 64 * 1024 * 1024
MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
MAX_MULTIPART_PART_SIZE = 5 * 1024 * 1024 * 1024
MAX_MULTIPART_OBJECT_SIZE = 5 * 1024 * 1024 * 1024 * 1024
JsonObject = dict[str, Any]
ProgressCallback = Callable[[int, int], None]

# Public contract operations used by this first SDK surface. Contract tests
# verify both the method/path and stable OpenAPI operation ID.
SDK_OPERATIONS: dict[str, tuple[str, str, str]] = {
    "get_identity": ("get", "/auth/me", "me"),
    "get_my_usage": ("get", "/auth/me/usage", "get_my_usage"),
    "get_my_organization": ("get", "/auth/me/organization", "get_my_organization"),
    "list_projects": ("get", "/projects", "list_projects"),
    "create_project": ("post", "/projects", "create_project"),
    "get_project": ("get", "/projects/{project_id}", "get_project"),
    "update_project": ("patch", "/projects/{project_id}", "update_project"),
    "delete_project": ("delete", "/projects/{project_id}", "delete_project"),
    "archive_project": ("post", "/projects/{project_id}/archive", "archive_project"),
    "unarchive_project": (
        "post",
        "/projects/{project_id}/unarchive",
        "unarchive_project",
    ),
    "list_jobs": ("get", "/jobs", "list_jobs"),
    "list_project_jobs": ("get", "/projects/{project_id}/jobs", "list_project_jobs"),
    "create_job": ("post", "/jobs", "create_job"),
    "get_job": ("get", "/jobs/{job_id}", "get_job"),
    "update_job": ("patch", "/jobs/{job_id}", "update_job"),
    "delete_job": ("delete", "/jobs/{job_id}", "delete_job"),
    "cancel_job": ("post", "/jobs/{job_id}/cancel", "cancel_job"),
    "upload": ("post", "/upload", "upload"),
    "init_presigned_multipart_upload": (
        "post",
        "/upload/init-presigned-multipart",
        "init_presigned_multipart_upload",
    ),
    "presign_multipart_part": (
        "post",
        "/upload/presign-part",
        "presign_multipart_part",
    ),
    "complete_presigned_multipart_upload": (
        "post",
        "/upload/complete-presigned-multipart",
        "complete_presigned_multipart_upload",
    ),
    "list_project_trees": ("get", "/projects/{project_id}/trees", "list_project_trees"),
    "get_project_tree": (
        "get",
        "/projects/{project_id}/trees/{tree_id}",
        "get_project_tree",
    ),
    "list_project_poles": ("get", "/projects/{project_id}/poles", "list_project_poles"),
    "list_project_spans": ("get", "/projects/{project_id}/spans", "list_project_spans"),
    "create_project_export": (
        "post",
        "/projects/{project_id}/exports",
        "create_project_export",
    ),
    "get_project_export": (
        "get",
        "/projects/{project_id}/exports/{export_id}",
        "get_project_export",
    ),
    "download_job_table": (
        "get",
        "/jobs/{job_id}/tables/{table}",
        "download_job_table",
    ),
    "download_job_folder": (
        "get",
        "/jobs/{job_id}/folder-zip",
        "download_job_folder_zip",
    ),
    "download_project_folder": (
        "get",
        "/projects/{project_id}/folder-zip",
        "download_project_folder_zip",
    ),
}


class Kanopy:
    """Client for Kanopy projects, inspections, inventory, and exports."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.BaseTransport | None = None,
        upload_transport: httpx.BaseTransport | None = None,
        upload_timeout: float | httpx.Timeout = 300.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        normalized_base_url = base_url.rstrip("/") + "/"
        self._client = httpx.Client(
            base_url=normalized_base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "kanopy-ai-python/0.1.0",
            },
        )
        self._upload_client = httpx.Client(
            timeout=upload_timeout,
            transport=upload_transport,
            follow_redirects=False,
        )

    def __enter__(self) -> "Kanopy":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._upload_client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path.lstrip("/"), **kwargs)
        if response.is_error:
            raise KanopyError.from_response(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @staticmethod
    def _object(value: Any) -> JsonObject:
        if not isinstance(value, dict):
            raise TypeError("Kanopy API returned an object with an unexpected shape")
        return value

    @staticmethod
    def _page(response: httpx.Response) -> Page[JsonObject]:
        value = response.json()
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise TypeError("Kanopy API returned a collection with an unexpected shape")
        total_raw = response.headers.get("X-Total-Count")
        total = int(total_raw) if total_raw is not None else None
        return Page(
            items=value,
            next_cursor=response.headers.get("X-Next-Cursor"),
            total_count=total,
        )

    @staticmethod
    def _pagination_params(*, skip: int, limit: int, cursor: str | None) -> JsonObject:
        params: JsonObject = {"limit": limit}
        if cursor is None:
            params["skip"] = skip
        else:
            params["cursor"] = cursor
        return params

    # Identity

    def get_identity(self) -> JsonObject:
        return self._object(self._json("GET", "/auth/me"))

    def get_my_usage(self) -> JsonObject:
        return self._object(self._json("GET", "/auth/me/usage"))

    def get_my_organization(self) -> JsonObject:
        return self._object(self._json("GET", "/auth/me/organization"))

    # Projects

    def list_projects(
        self, *, skip: int = 0, limit: int = 50, cursor: str | None = None
    ) -> Page[JsonObject]:
        response = self._request(
            "GET",
            "/projects",
            params=self._pagination_params(skip=skip, limit=limit, cursor=cursor),
        )
        return self._page(response)

    def create_project(
        self, *, name: str, description: str | None = None
    ) -> JsonObject:
        payload: JsonObject = {"name": name}
        if description is not None:
            payload["description"] = description
        return self._object(self._json("POST", "/projects", json=payload))

    def get_project(self, project_id: str) -> JsonObject:
        return self._object(self._json("GET", f"/projects/{project_id}"))

    def update_project(self, project_id: str, **changes: Any) -> JsonObject:
        return self._object(
            self._json("PATCH", f"/projects/{project_id}", json=changes)
        )

    def delete_project(self, project_id: str) -> None:
        self._json("DELETE", f"/projects/{project_id}")

    def archive_project(self, project_id: str) -> JsonObject:
        return self._object(self._json("POST", f"/projects/{project_id}/archive"))

    def unarchive_project(self, project_id: str) -> JsonObject:
        return self._object(self._json("POST", f"/projects/{project_id}/unarchive"))

    # Jobs

    def list_jobs(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        cursor: str | None = None,
        project_id: str | None = None,
    ) -> Page[JsonObject]:
        params = self._pagination_params(skip=skip, limit=limit, cursor=cursor)
        if project_id is not None:
            params["project_id"] = project_id
        return self._page(self._request("GET", "/jobs", params=params))

    def list_project_jobs(
        self, project_id: str, *, skip: int = 0, limit: int = 50
    ) -> Page[JsonObject]:
        params = {"skip": skip, "limit": limit}
        return self._page(
            self._request("GET", f"/projects/{project_id}/jobs", params=params)
        )

    def create_job(
        self,
        *,
        title: str,
        project_id: str | None = None,
        upload_request_id: str | None = None,
        **options: Any,
    ) -> JsonObject:
        payload: JsonObject = {"title": title, **options}
        if project_id is not None:
            payload["project_uuid"] = project_id
        if upload_request_id is not None:
            payload["upload_request_id"] = upload_request_id
        return self._object(self._json("POST", "/jobs", json=payload))

    def get_job(self, job_id: str) -> JsonObject:
        return self._object(self._json("GET", f"/jobs/{job_id}"))

    def update_job(self, job_id: str, **changes: Any) -> JsonObject:
        return self._object(self._json("PATCH", f"/jobs/{job_id}", json=changes))

    def delete_job(self, job_id: str) -> None:
        self._json("DELETE", f"/jobs/{job_id}")

    def cancel_job(self, job_id: str) -> JsonObject:
        return self._object(self._json("POST", f"/jobs/{job_id}/cancel"))

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 3600,
        poll_interval: float = 5,
        terminal_statuses: Iterable[str] = ("completed", "failed", "canceled"),
    ) -> JsonObject:
        terminal = {status.lower() for status in terminal_statuses}
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if str(job.get("status", "")).lower() in terminal:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Kanopy job {job_id}")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    # Upload

    def upload(
        self,
        video: str | PathLike[str] | BinaryIO,
        *,
        metadata: str | PathLike[str] | BinaryIO | None = None,
        project_id: str | None = None,
        title: str | None = None,
        job_id: str | None = None,
        upload_request_id: str | None = None,
        **fields: Any,
    ) -> JsonObject:
        data: dict[str, str] = {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in fields.items()
            if value is not None
        }
        if upload_request_id is not None and job_id is None:
            if title is None:
                raise ValueError(
                    "title is required when upload_request_id creates an idempotent job"
                )
            created = self.create_job(
                title=title,
                project_id=project_id,
                upload_request_id=upload_request_id,
            )
            job_id = str(created["id"])

        if job_id is not None:
            data["job_id"] = job_id
        if project_id is not None:
            data["project_uuid"] = project_id
        if title is not None:
            data["title"] = title

        with ExitStack() as stack:
            video_name, video_file = self._open_upload(video, stack)
            files: dict[str, tuple[str, BinaryIO, str]] = {
                "video": (video_name, video_file, "video/mp4")
            }
            if metadata is not None:
                metadata_name, metadata_file = self._open_upload(metadata, stack)
                files["metadata"] = (
                    metadata_name,
                    metadata_file,
                    "application/octet-stream",
                )
            return self._object(self._json("POST", "/upload", data=data, files=files))

    def init_presigned_multipart_upload(self, **job_options: Any) -> JsonObject:
        """Create or resume a Kanopy multipart upload session."""
        return self._object(
            self._json("POST", "/upload/init-presigned-multipart", json=job_options)
        )

    def presign_multipart_part(
        self,
        *,
        job_id: str,
        s3_key: str,
        upload_id: str,
        part_number: int,
    ) -> JsonObject:
        return self._object(
            self._json(
                "POST",
                "/upload/presign-part",
                json={
                    "job_id": job_id,
                    "s3_key": s3_key,
                    "upload_id": upload_id,
                    "part_number": part_number,
                },
            )
        )

    def complete_presigned_multipart_upload(
        self,
        *,
        job_id: str,
        s3_key: str,
        upload_id: str,
        parts: Sequence[Mapping[str, Any]],
        metadata: str | PathLike[str] | BinaryIO | None = None,
        metadata_files: Sequence[str | PathLike[str] | BinaryIO] = (),
        gps_track: str | PathLike[str] | BinaryIO | None = None,
        original_filename: str | None = None,
        completion_fields: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """Finalize uploaded S3 parts and automatically queue processing."""
        data: dict[str, str] = {
            "job_id": job_id,
            "s3_key": s3_key,
            "upload_id": upload_id,
            "parts_json": json.dumps(list(parts)),
        }
        for key, value in (completion_fields or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                data[key] = json.dumps(value)
            elif isinstance(value, bool):
                data[key] = str(value).lower()
            else:
                data[key] = str(value)
        if original_filename is not None:
            data["original_filename"] = original_filename

        with ExitStack() as stack:
            # This endpoint declares File parameters, so force multipart even
            # when no sidecars are present. httpx otherwise sends urlencoded
            # form data, which does not match the published request contract.
            files: list[tuple[str, Any]] = [
                (key, (None, value)) for key, value in data.items()
            ]
            if metadata is not None:
                name, file_obj = self._open_upload(metadata, stack)
                files.append(("metadata", (name, file_obj, "application/octet-stream")))
            for item in metadata_files:
                name, file_obj = self._open_upload(item, stack)
                files.append(
                    ("metadata_files", (name, file_obj, "application/octet-stream"))
                )
            if gps_track is not None:
                name, file_obj = self._open_upload(gps_track, stack)
                files.append(("gps_track_file", (name, file_obj, "application/json")))
            return self._object(
                self._json(
                    "POST",
                    "/upload/complete-presigned-multipart",
                    files=files,
                )
            )

    def upload_multipart(
        self,
        video: str | PathLike[str],
        *,
        metadata: str | PathLike[str] | BinaryIO | None = None,
        metadata_files: Sequence[str | PathLike[str] | BinaryIO] = (),
        gps_track: str | PathLike[str] | BinaryIO | None = None,
        project_id: str | None = None,
        title: str | None = None,
        upload_request_id: str | None = None,
        content_type: str = "video/mp4",
        part_size: int = DEFAULT_MULTIPART_PART_SIZE,
        max_workers: int = 4,
        part_retries: int = 3,
        progress: ProgressCallback | None = None,
        completion_fields: Mapping[str, Any] | None = None,
        **job_options: Any,
    ) -> JsonObject:
        """Upload a large video directly to storage and queue reconstruction.

        ``job_options`` maps to the remaining public multipart-init fields,
        including capture, circuit, voltage, and clearance settings.
        """
        path = Path(video)
        total_size = path.stat().st_size
        if total_size <= 0:
            raise ValueError("video must not be empty")
        if total_size > MAX_MULTIPART_OBJECT_SIZE:
            raise ValueError("video exceeds the 5 TiB multipart object limit")
        if part_size < MIN_MULTIPART_PART_SIZE:
            raise ValueError(
                f"part_size must be at least {MIN_MULTIPART_PART_SIZE} bytes (5 MiB)"
            )
        if part_size > MAX_MULTIPART_PART_SIZE:
            raise ValueError("part_size must not exceed 5 GiB")
        if max_workers < 1 or max_workers > 32:
            raise ValueError("max_workers must be in the range 1..32")
        if part_retries < 1:
            raise ValueError("part_retries must be at least 1")

        required_part_size = math.ceil(total_size / MAX_MULTIPART_PARTS)
        effective_part_size = max(part_size, required_part_size)
        part_count = math.ceil(total_size / effective_part_size)

        init_options = dict(job_options)
        init_options["title"] = title or path.stem
        init_options["content_type"] = content_type
        init_options["content_length"] = total_size
        if project_id is not None:
            init_options["project_uuid"] = project_id
        if upload_request_id is not None:
            init_options["upload_request_id"] = upload_request_id
        initialized = self.init_presigned_multipart_upload(**init_options)
        job_id = str(initialized["job_id"])
        s3_key = str(initialized["s3_key"])
        upload_id = str(initialized["upload_id"])

        uploaded: list[dict[str, Any]] = []
        transferred = 0
        with ThreadPoolExecutor(max_workers=min(max_workers, part_count)) as executor:
            futures = {
                executor.submit(
                    self._upload_part,
                    path,
                    offset=(part_number - 1) * effective_part_size,
                    size=min(
                        effective_part_size,
                        total_size - (part_number - 1) * effective_part_size,
                    ),
                    job_id=job_id,
                    s3_key=s3_key,
                    upload_id=upload_id,
                    part_number=part_number,
                    attempts=part_retries,
                ): part_number
                for part_number in range(1, part_count + 1)
            }
            for future in as_completed(futures):
                part, byte_count = future.result()
                uploaded.append(part)
                transferred += byte_count
                if progress is not None:
                    progress(transferred, total_size)

        uploaded.sort(key=lambda part: int(part["PartNumber"]))
        return self.complete_presigned_multipart_upload(
            job_id=job_id,
            s3_key=s3_key,
            upload_id=upload_id,
            parts=uploaded,
            metadata=metadata,
            metadata_files=metadata_files,
            gps_track=gps_track,
            original_filename=path.name,
            completion_fields=completion_fields,
        )

    def upload_large(self, video: str | PathLike[str], **kwargs: Any) -> JsonObject:
        """Friendly alias for :meth:`upload_multipart`."""
        return self.upload_multipart(video, **kwargs)

    def _upload_part(
        self,
        path: Path,
        *,
        offset: int,
        size: int,
        job_id: str,
        s3_key: str,
        upload_id: str,
        part_number: int,
        attempts: int,
    ) -> tuple[dict[str, Any], int]:
        with path.open("rb") as source:
            source.seek(offset)
            content = source.read(size)
        if len(content) != size:
            raise KanopyUploadError(
                f"Unable to read multipart upload part {part_number}",
                part_number=part_number,
            )

        last_status: int | None = None
        last_message = "upload failed"
        for attempt in range(1, attempts + 1):
            signed = self.presign_multipart_part(
                job_id=job_id,
                s3_key=s3_key,
                upload_id=upload_id,
                part_number=part_number,
            )
            try:
                response = self._upload_client.put(str(signed["url"]), content=content)
                last_status = response.status_code
                etag = response.headers.get("ETag")
                if response.is_success and etag:
                    return {"PartNumber": part_number, "ETag": etag}, size
                last_message = (
                    f"storage returned HTTP {response.status_code}"
                    if not response.is_success
                    else "storage response did not include an ETag"
                )
            except httpx.HTTPError as exc:
                last_message = str(exc)
            if attempt < attempts:
                time.sleep(0.5 * (2 ** (attempt - 1)))

        raise KanopyUploadError(
            f"Multipart part {part_number} failed after {attempts} attempt(s): {last_message}",
            part_number=part_number,
            status_code=last_status,
        )

    @staticmethod
    def _open_upload(
        value: str | PathLike[str] | BinaryIO, stack: ExitStack
    ) -> tuple[str, BinaryIO]:
        if isinstance(value, (str, PathLike)):
            path = Path(value)
            return path.name, stack.enter_context(path.open("rb"))
        name = Path(str(getattr(value, "name", "upload.bin"))).name
        if hasattr(value, "seek"):
            value.seek(0)
        return name, value

    # Inventory

    def _inventory(self, kind: str, project_id: str, **filters: Any) -> JsonObject:
        params = {key: value for key, value in filters.items() if value is not None}
        return self._object(
            self._json("GET", f"/projects/{project_id}/{kind}", params=params)
        )

    def list_project_trees(self, project_id: str, **filters: Any) -> JsonObject:
        return self._inventory("trees", project_id, **filters)

    def get_project_tree(self, project_id: str, tree_id: str) -> JsonObject:
        return self._object(
            self._json("GET", f"/projects/{project_id}/trees/{tree_id}")
        )

    def list_project_poles(self, project_id: str, **filters: Any) -> JsonObject:
        return self._inventory("poles", project_id, **filters)

    def list_project_spans(self, project_id: str, **filters: Any) -> JsonObject:
        return self._inventory("spans", project_id, **filters)

    # Exports and downloads

    def create_project_export(self, project_id: str) -> JsonObject:
        return self._object(self._json("POST", f"/projects/{project_id}/exports"))

    def get_project_export(self, project_id: str, export_id: str) -> JsonObject:
        return self._object(
            self._json("GET", f"/projects/{project_id}/exports/{export_id}")
        )

    def download_job_table(
        self, job_id: str, table: str, destination: str | PathLike[str]
    ) -> Path:
        return self._download(f"/jobs/{job_id}/tables/{table}", destination)

    def download_job_folder(
        self,
        job_id: str,
        destination: str | PathLike[str],
        *,
        include: str | None = None,
        point_cloud_epsg: int | None = None,
    ) -> Path:
        params = {"include": include, "point_cloud_epsg": point_cloud_epsg}
        return self._download(f"/jobs/{job_id}/folder-zip", destination, params=params)

    def download_project_folder(
        self, project_id: str, destination: str | PathLike[str]
    ) -> Path:
        return self._download(f"/projects/{project_id}/folder-zip", destination)

    def _download(
        self,
        path: str,
        destination: str | PathLike[str],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Path:
        filtered_params = (
            {key: value for key, value in params.items() if value is not None}
            if params
            else None
        )
        target = Path(destination)
        with self._client.stream(
            "GET", path.lstrip("/"), params=filtered_params
        ) as response:
            if response.is_error:
                response.read()
                raise KanopyError.from_response(response)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        return target
