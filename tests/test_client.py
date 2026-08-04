from __future__ import annotations

import io
import json

import httpx
import pytest

from kanopy import Kanopy, KanopyError, KanopyUploadError


def test_client_composes_versioned_base_url_and_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "project-1", "name": "North"})

    with Kanopy(
        "kpy_test_secret",
        base_url="https://example.test/api/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        project = client.get_project("project-1")

    assert project["id"] == "project-1"
    assert str(requests[0].url) == "https://example.test/api/v1/projects/project-1"
    assert requests[0].headers["Authorization"] == "Bearer kpy_test_secret"


def test_cursor_page_preserves_contract_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cursor"] == ""
        assert "skip" not in request.url.params
        return httpx.Response(
            200,
            json=[{"id": "job-1"}],
            headers={"X-Next-Cursor": "next-page", "X-Total-Count": "12"},
        )

    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        page = client.list_jobs(cursor="", limit=100)

    assert page.items == [{"id": "job-1"}]
    assert page.next_cursor == "next-page"
    assert page.total_count == 12
    assert page.has_next


def test_api_error_exposes_kanopy_error_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": "State conflict",
                "code": "conflict",
                "detail": {"status": "processing"},
                "request_id": "request-123",
            },
        )

    with (
        Kanopy("key", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(KanopyError) as caught,
    ):
        client.cancel_job("job-1")

    error = caught.value
    assert str(error) == "State conflict"
    assert error.status_code == 409
    assert error.code == "conflict"
    assert error.detail == {"status": "processing"}
    assert error.request_id == "request-123"


def test_upload_queues_processing_without_manual_submit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/jobs":
            return httpx.Response(201, json={"id": "job-1", "status": "uploading"})
        return httpx.Response(201, json={"job_id": "job-1", "status": "pending"})

    video = io.BytesIO(b"video")
    video.name = "flight.mp4"
    metadata = io.BytesIO(b"metadata")
    metadata.name = "flight.csv"

    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        result = client.upload(
            video,
            metadata=metadata,
            capture_device="drone",
            project_id="project-1",
            title="Flight 1",
            upload_request_id="flight-1",
            line_clearance=True,
        )

    assert result == {"job_id": "job-1", "status": "pending"}
    assert [request.url.path for request in requests] == [
        "/api/v1/jobs",
        "/api/v1/upload",
    ]
    assert json.loads(requests[0].content)["upload_request_id"] == "flight-1"
    body = requests[1].content
    assert b'form-data; name="job_id"' in body
    assert b"job-1" in body
    assert b'form-data; name="project_uuid"' in body
    assert b"project-1" in body
    assert b'filename="flight.mp4"' in body
    assert b'form-data; name="capture_device"' in body
    assert b"drone" in body


def test_upload_validates_declared_capture_device_sidecars() -> None:
    video = io.BytesIO(b"video")
    video.name = "flight.mp4"
    srt = io.BytesIO(b"subtitle telemetry")
    srt.name = "flight.srt"

    with Kanopy(
        "key", transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="require a .csv or .txt"):
            client.upload(video, capture_device="drone")
        with pytest.raises(ValueError, match="unsupported: flight.srt"):
            client.upload(video, metadata=srt, capture_device="drone")
        with pytest.raises(ValueError, match="capture_device must be one of"):
            client.upload(video, capture_device="gopro")


def test_wait_for_job_stops_at_terminal_status() -> None:
    statuses = iter(["pending", "processing", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job-1", "status": next(statuses)})

    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        job = client.wait_for_job("job-1", timeout=1, poll_interval=0)

    assert job["status"] == "completed"


def test_download_streams_to_destination(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"tree_id,risk\n1,high\n")

    destination = tmp_path / "trees.csv"
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        result = client.download_job_table("job-1", "trees", destination)

    assert result == destination
    assert destination.read_bytes() == b"tree_id,risk\n1,high\n"


def test_download_project_export_waits_then_fetches_presigned_url(tmp_path) -> None:
    polls = iter(["running", "completed"])
    api_requests: list[httpx.Request] = []
    storage_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "export-1", "status": "queued"})
        status = next(polls)
        return httpx.Response(
            200,
            json={
                "id": "export-1",
                "status": status,
                "download_url": (
                    "https://storage.test/project.zip"
                    if status == "completed"
                    else None
                ),
            },
        )

    def storage_handler(request: httpx.Request) -> httpx.Response:
        storage_requests.append(request)
        return httpx.Response(200, content=b"project archive")

    destination = tmp_path / "project.zip"
    with Kanopy(
        "key",
        transport=httpx.MockTransport(api_handler),
        upload_transport=httpx.MockTransport(storage_handler),
    ) as client:
        result = client.download_project_export(
            "project-1", destination, timeout=1, poll_interval=0
        )

    assert result == destination
    assert destination.read_bytes() == b"project archive"
    assert [request.method for request in api_requests] == ["POST", "GET", "GET"]
    assert storage_requests[0].headers.get("Authorization") is None


def test_download_audit_events_maps_staff_and_date_filters(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"created_at,action\n")

    destination = tmp_path / "staff-access.csv"
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        client.download_audit_events(
            destination,
            staff_only=True,
            start="2026-07-01T00:00:00Z",
            end="2026-08-01T00:00:00Z",
        )

    params = requests[0].url.params
    assert params["actor_is_staff"] == "true"
    assert params["from"] == "2026-07-01T00:00:00Z"
    assert params["to"] == "2026-08-01T00:00:00Z"


@pytest.mark.parametrize("format", ["csv", "json", "kml", "geojson"])
def test_download_project_tree_formats(tmp_path, format: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "500"
        return httpx.Response(
            200,
            json={
                "trees": [
                    {
                        "tree_id": "tree-1",
                        "latitude": 41.5,
                        "longitude": -87.6,
                        "risk_level": "High",
                        "job_ids": ["job-1"],
                    }
                ],
                "total": 1,
                "skip": 0,
                "limit": 500,
            },
        )

    destination = tmp_path / f"trees.{format}"
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        client.download_project_table("project-1", "trees", destination, format=format)

    content = destination.read_text()
    assert "tree-1" in content
    if format == "geojson":
        assert json.loads(content)["features"][0]["geometry"]["coordinates"] == [
            -87.6,
            41.5,
        ]
    if format == "kml":
        assert "<coordinates>-87.6,41.5</coordinates>" in content


def test_download_project_table_rejects_ui_unsupported_format(tmp_path) -> None:
    with Kanopy("key") as client, pytest.raises(ValueError, match="poles format"):
        client.download_project_table(
            "project-1", "poles", tmp_path / "poles.kml", format="kml"
        )


def test_download_tree_report_generates_pdf_from_public_record(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["include_all_frames"] == "true"
        assert request.url.params["include_veg_analyses"] == "true"
        return httpx.Response(
            200,
            json={
                "tree_id": "tree-1",
                "status": "active",
                "risk_level": "High",
                "risk_score": 82.5,
                "latitude": 41.5,
                "longitude": -87.6,
                "min_absolute_distance_m": 1.2,
                "veg_analyses": [{"summary": "Priority pruning recommended"}],
                "frames": [],
                "all_frames": [],
            },
        )

    destination = tmp_path / "tree-analysis.pdf"
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        result = client.download_tree_report("project-1", "tree-1", destination)

    assert result == destination
    assert destination.read_bytes().startswith(b"%PDF-")


def test_large_upload_sends_ordered_parts_and_queues_job(tmp_path) -> None:
    video = tmp_path / "large-flight.mp4"
    video.write_bytes(b"a" * (5 * 1024 * 1024) + b"tail")
    api_requests: list[httpx.Request] = []
    storage_requests: list[httpx.Request] = []
    progress: list[tuple[int, int]] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        if request.url.path.endswith("/init-presigned-multipart"):
            payload = json.loads(request.content)
            assert payload["project_uuid"] == "project-1"
            assert payload["upload_request_id"] == "flight-large-1"
            assert payload["content_length"] == video.stat().st_size
            assert payload["capture_device"] == "action_cam"
            return httpx.Response(
                201,
                json={
                    "job_id": "job-1",
                    "s3_key": "tenants/org/jobs/job-1/video.mp4",
                    "upload_id": "upload-1",
                    "content_type": "video/mp4",
                },
            )
        if request.url.path.endswith("/presign-part"):
            part_number = json.loads(request.content)["part_number"]
            return httpx.Response(
                200,
                json={
                    "url": f"https://storage.test/parts/{part_number}",
                    "part_number": part_number,
                },
            )
        if request.url.path.endswith("/complete-presigned-multipart"):
            assert b'form-data; name="parts_json"' in request.content
            first = request.content.index(b'"PartNumber": 1')
            second = request.content.index(b'"PartNumber": 2')
            assert first < second
            assert b'form-data; name="transcode_config"' in request.content
            assert b"serverSideUploadPrepRequested" in request.content
            assert b"sourceVideoPreserved" in request.content
            return httpx.Response(200, json={"job_id": "job-1", "status": "pending"})
        raise AssertionError(f"Unexpected API request: {request.url}")

    def storage_handler(request: httpx.Request) -> httpx.Response:
        storage_requests.append(request)
        part_number = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, headers={"ETag": f'"etag-{part_number}"'})

    with Kanopy(
        "key",
        transport=httpx.MockTransport(api_handler),
        upload_transport=httpx.MockTransport(storage_handler),
    ) as client:
        result = client.upload_large(
            video,
            project_id="project-1",
            title="Large flight",
            upload_request_id="flight-large-1",
            capture_device="action_cam",
            part_size=5 * 1024 * 1024,
            max_workers=2,
            part_retries=1,
            progress=lambda sent, total: progress.append((sent, total)),
        )

    assert result == {"job_id": "job-1", "status": "pending"}
    assert sorted(len(request.content) for request in storage_requests) == [
        4,
        5 * 1024 * 1024,
    ]
    assert progress[-1] == (video.stat().st_size, video.stat().st_size)
    assert not any(request.url.path.endswith("/submit") for request in api_requests)


def test_large_drone_upload_requires_supported_flight_log(tmp_path) -> None:
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"video")
    srt = tmp_path / "flight.srt"
    srt.write_text("telemetry")

    with Kanopy(
        "key", transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="require a .csv or .txt"):
            client.upload_large(video, capture_device="drone")
        with pytest.raises(ValueError, match="unsupported: flight.srt"):
            client.upload_large(video, metadata=srt, capture_device="drone")


def test_large_phone_upload_does_not_request_server_prep_by_default(tmp_path) -> None:
    video = tmp_path / "phone.mp4"
    video.write_bytes(b"video")
    completion_bodies: list[bytes] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init-presigned-multipart"):
            return httpx.Response(
                201,
                json={
                    "job_id": "job-1",
                    "s3_key": "video.mp4",
                    "upload_id": "upload-1",
                    "content_type": "video/mp4",
                },
            )
        if request.url.path.endswith("/presign-part"):
            return httpx.Response(
                200,
                json={"url": "https://storage.test/part", "part_number": 1},
            )
        completion_bodies.append(request.content)
        return httpx.Response(200, json={"job_id": "job-1", "status": "pending"})

    with Kanopy(
        "key",
        transport=httpx.MockTransport(api_handler),
        upload_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"ETag": '"etag"'})
        ),
    ) as client:
        client.upload_large(
            video,
            capture_device="phone",
            part_size=5 * 1024 * 1024,
        )

    assert b"serverSideUploadPrepRequested" not in completion_bodies[0]


def test_large_upload_represigns_and_retries_failed_part(tmp_path, monkeypatch) -> None:
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"video")
    presign_count = 0
    storage_count = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal presign_count
        if request.url.path.endswith("/init-presigned-multipart"):
            return httpx.Response(
                201,
                json={
                    "job_id": "job-1",
                    "s3_key": "video.mp4",
                    "upload_id": "upload-1",
                    "content_type": "video/mp4",
                },
            )
        if request.url.path.endswith("/presign-part"):
            presign_count += 1
            return httpx.Response(
                200,
                json={
                    "url": f"https://storage.test/attempt/{presign_count}",
                    "part_number": 1,
                },
            )
        return httpx.Response(200, json={"job_id": "job-1", "status": "pending"})

    def storage_handler(request: httpx.Request) -> httpx.Response:
        nonlocal storage_count
        storage_count += 1
        if storage_count == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"ETag": '"recovered"'})

    monkeypatch.setattr("kanopy.client.time.sleep", lambda _: None)
    with Kanopy(
        "key",
        transport=httpx.MockTransport(api_handler),
        upload_transport=httpx.MockTransport(storage_handler),
    ) as client:
        result = client.upload_large(
            video,
            part_size=5 * 1024 * 1024,
            max_workers=1,
            part_retries=2,
        )

    assert result["status"] == "pending"
    assert presign_count == 2
    assert storage_count == 2


def test_large_upload_rejects_invalid_part_size(tmp_path) -> None:
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"video")

    with Kanopy(
        "key", transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="5 MiB"):
            client.upload_large(video, part_size=1024)

        with pytest.raises(ValueError, match="5 GiB"):
            client.upload_large(video, part_size=6 * 1024 * 1024 * 1024)


def test_large_upload_reports_part_failure(tmp_path) -> None:
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"video")

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init-presigned-multipart"):
            return httpx.Response(
                201,
                json={
                    "job_id": "job-1",
                    "s3_key": "video.mp4",
                    "upload_id": "upload-1",
                    "content_type": "video/mp4",
                },
            )
        return httpx.Response(
            200,
            json={"url": "https://storage.test/part", "part_number": 1},
        )

    with (
        Kanopy(
            "key",
            transport=httpx.MockTransport(api_handler),
            upload_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ) as client,
        pytest.raises(KanopyUploadError) as caught,
    ):
        client.upload_large(
            video,
            part_size=5 * 1024 * 1024,
            max_workers=1,
            part_retries=1,
        )

    assert caught.value.part_number == 1
    assert caught.value.status_code == 500


def test_list_job_outputs_returns_the_outputs_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/jobs/job-1/outputs"
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "outputs": [
                    {
                        "id": "trees_table",
                        "kind": "trees_table",
                        "format": "csv",
                        "size_bytes": None,
                        "version": "2026-07-30T14:03:11",
                        "download_url": "https://api.test/api/v1/jobs/job-1/outputs/trees_table",
                    }
                ],
            },
        )

    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        outputs = client.list_job_outputs("job-1")

    assert [output["id"] for output in outputs] == ["trees_table"]


def test_list_job_outputs_is_empty_for_a_job_with_no_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "job-1", "outputs": []})

    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        assert client.list_job_outputs("job-1") == []


def test_download_job_output_streams_inline_bytes(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/jobs/job-1/outputs/trees_table"
        return httpx.Response(200, content=b"tree_id,risk\n1,high\n")

    destination = tmp_path / "trees.csv"
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        result = client.download_job_output("job-1", "trees_table", destination)

    assert result == destination
    assert destination.read_bytes() == b"tree_id,risk\n1,high\n"


def test_download_job_output_follows_presigned_redirect_without_the_api_key(
    tmp_path,
) -> None:
    storage_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://storage.test/merged.ply?sig=abc"}
        )

    def storage_handler(request: httpx.Request) -> httpx.Response:
        storage_requests.append(request)
        return httpx.Response(200, content=b"ply bytes")

    destination = tmp_path / "merged.ply"
    with Kanopy(
        "key",
        transport=httpx.MockTransport(api_handler),
        upload_transport=httpx.MockTransport(storage_handler),
    ) as client:
        result = client.download_job_output("job-1", "merged_point_cloud", destination)

    assert result == destination
    assert destination.read_bytes() == b"ply bytes"
    # The bearer token must never reach the storage host.
    assert "authorization" not in {key.lower() for key in storage_requests[0].headers}
