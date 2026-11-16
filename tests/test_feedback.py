"""Feedback uses body pagination and caller-owned submission deduplication."""

import json
from uuid import UUID

import httpx
import pytest

from kanopy import Kanopy, KanopyError

REQUEST_ID = UUID("fa3e8d9a-8c90-4ddc-b9c4-227626da3b34")
ORG_ID = "00000000-0000-0000-0000-000000000001"


def test_feedback_reads_authenticate_and_use_public_paths():
    requests = []
    taxonomy = {"schema_version": 1, "categories": [{"code": "other"}]}
    report = {"id": "report", "status": "open"}

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=taxonomy if request.url.path.endswith("taxonomy") else report
        )

    with Kanopy(
        "secret", organization_id=ORG_ID, transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_feedback_taxonomy() == taxonomy
        assert client.get_project_feedback("project", "report") == report
    assert [r.url.path for r in requests] == [
        "/api/v1/feedback/taxonomy",
        "/api/v1/projects/project/feedback/report",
    ]
    assert all(r.headers["Authorization"] == "Bearer secret" for r in requests)
    assert all(r.headers["X-Kanopy-Organization-Id"] == ORG_ID for r in requests)


def test_feedback_pagination_reads_body_cursor_and_preserves_filters():
    requests = []

    def handler(request):
        requests.append(request)
        second = "cursor" in request.url.params
        return httpx.Response(
            200,
            json={
                "items": [{"id": "second" if second else "first"}],
                "next_cursor": None if second else "body-cursor",
            },
            headers={"X-Next-Cursor": "wrong-header", "X-Total-Count": "99"},
        )

    filters = {
        "asset_id": "tree",
        "job_id": "job",
        "status": "open",
        "category": "location",
    }
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        first = client.list_project_feedback("project", limit=1, **filters)
        assert first.has_next
        second = client.list_project_feedback(
            "project", limit=1, cursor=first.next_cursor, **filters
        )
    assert [row["id"] for row in first.items + second.items] == ["first", "second"]
    assert first.total_count is None
    assert not second.has_next
    assert dict(requests[0].url.params) == {**filters, "limit": "1"}
    assert dict(requests[1].url.params) == {
        **filters,
        "limit": "1",
        "cursor": "body-cursor",
    }


@pytest.mark.parametrize("request_id", [REQUEST_ID, str(REQUEST_ID)])
def test_submission_keeps_request_id_across_application_retries(
    request_id, monkeypatch
):
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost after submission", request=request)
        return httpx.Response(201, json={"id": "same-report"})

    monkeypatch.setattr("kanopy.client.time.sleep", sleeps.append)
    payload = {
        "request_id": request_id,
        "category": "other",
        "problem_type": "other",
        "note": "A problem",
    }
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.submit_project_feedback("project", **payload)
        assert len(requests) == 1
        assert (
            client.submit_project_feedback("project", **payload)["id"] == "same-report"
        )
    assert sleeps == []
    assert requests[0].content == requests[1].content
    assert json.loads(requests[0].content) == {
        **payload,
        "request_id": str(REQUEST_ID),
        "source": "api",
    }
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/projects/project/feedback"


def test_submission_preserves_optional_observation_context_and_zero_indices():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"id": "report"})

    payload = {
        "request_id": str(REQUEST_ID),
        "category": "location",
        "problem_type": "incorrect",
        "note": "Location is wrong",
        "asset_type": "tree",
        "asset_id": "tree",
        "job_id": "job",
        "observation_id": "observation",
        "instance_id": 0,
        "chunk_index": 0,
        "source": "map",
        "expected_value": "Corrected location",
    }
    with Kanopy("key", transport=httpx.MockTransport(handler)) as client:
        client.submit_project_feedback("project", **payload)
    assert json.loads(requests[0].content) == payload


def test_feedback_validation_error_is_preserved_without_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(422, json={"detail": "Unsupported category combination"})

    with (
        Kanopy("key", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(KanopyError) as error,
    ):
        client.submit_project_feedback(
            "project",
            request_id=REQUEST_ID,
            category="invalid",
            problem_type="other",
            note="A problem",
        )
    assert error.value.status_code == 422
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload", [{"items": {}}, {"items": ["invalid"]}, {"items": [], "next_cursor": 42}]
)
def test_malformed_feedback_page_is_rejected(payload):
    with (
        Kanopy(
            "key",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client,
        pytest.raises((TypeError, ValueError)),
    ):
        client.list_project_feedback("project")
