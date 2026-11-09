from __future__ import annotations

import json
from pathlib import Path

from kanopy.client import SDK_OPERATIONS

CONTRACT = Path(__file__).parent / "fixtures/openapi.public.json"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_sdk_operations_exist_in_reviewed_public_contract() -> None:
    schema = _contract()
    for sdk_method, (method, path, operation_id) in SDK_OPERATIONS.items():
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id, sdk_method


def test_contract_server_and_paths_compose_without_duplicate_version_prefix() -> None:
    schema = _contract()
    assert schema["servers"] == [{"url": "/api/v1"}]
    assert all(path.startswith("/") for path in schema["paths"])
    assert not any(path.startswith("/api/v1/") for path in schema["paths"])


def test_every_sdk_operation_has_a_real_client_method() -> None:
    from kanopy import Kanopy

    assert all(
        callable(getattr(Kanopy, method_name, None)) for method_name in SDK_OPERATIONS
    )


def test_report_imagery_uses_reviewed_job_asset_operation() -> None:
    from kanopy.client import JOB_ASSET_PATH

    schema = _contract()
    operation = schema["paths"]["/jobs/{job_id}/asset/{asset_path}"]["get"]
    assert operation["operationId"] == "get_job_asset"
    frame = "/jobs/job-1/asset/frames_for_reconstruction/frame_000722.jpg"
    # Versioned mount and the legacy /api alias that frame imagePath uses.
    assert JOB_ASSET_PATH.fullmatch(schema["servers"][0]["url"] + frame)
    assert JOB_ASSET_PATH.fullmatch("/api" + frame)
