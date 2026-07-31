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
