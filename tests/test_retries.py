"""Retry controls let callers own API rate limiting and failure handling."""

import httpx
import pytest

from kanopy import Kanopy, KanopyError


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_zero_retries_returns_first_http_error_without_sleep(status, monkeypatch):
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "120"},
            json={"code": "rate_limited", "detail": "Try later"},
        )

    monkeypatch.setattr("kanopy.client.time.sleep", sleeps.append)
    with (
        Kanopy("key", max_retries=0, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(KanopyError) as error,
    ):
        client.get_project("project")
    assert error.value.status_code == status
    assert error.value.code == "rate_limited"
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("max_retries", [0, 1, 3])
@pytest.mark.parametrize("failure", ["http", "transport"])
def test_retry_budget_counts_additional_attempts(max_retries, failure, monkeypatch):
    attempts = []
    sleeps = []

    def handler(request):
        attempts.append(request)
        if failure == "transport":
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(503, headers={"Retry-After": "999"})

    monkeypatch.setattr("kanopy.client.time.sleep", sleeps.append)
    with (
        Kanopy(
            "key", max_retries=max_retries, transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(httpx.ConnectError if failure == "transport" else KanopyError),
    ):
        client.get_project("project")
    assert len(attempts) == max_retries + 1
    assert len(sleeps) == max_retries
    assert all(0 <= delay <= 120 for delay in sleeps)
    if failure == "http":
        assert sleeps == [120] * max_retries


def test_retry_configuration_is_per_client_and_preserves_default(monkeypatch):
    attempts = []
    monkeypatch.setattr("kanopy.client.time.sleep", lambda _: None)

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    with (
        Kanopy(
            "key", max_retries=0, transport=httpx.MockTransport(handler)
        ) as disabled,
        Kanopy("key", transport=httpx.MockTransport(handler)) as default,
    ):
        for client, count in ((disabled, 1), (default, 3), (disabled, 1)):
            attempts.clear()
            with pytest.raises(KanopyError):
                client.get_project("project")
            assert len(attempts) == count


@pytest.mark.parametrize("value", [-1, 1.5, "2", True, None])
def test_invalid_retry_configuration_is_rejected(value):
    with pytest.raises((TypeError, ValueError), match="non-negative integer"):
        Kanopy("key", max_retries=value)


def test_large_retry_budget_backoff_remains_capped():
    assert Kanopy._backoff_seconds(10_000) == 120


def test_custom_budget_does_not_enable_retries_for_mutations(monkeypatch):
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    monkeypatch.setattr("kanopy.client.time.sleep", sleeps.append)
    with (
        Kanopy("key", max_retries=4, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(KanopyError),
    ):
        client.create_project(name="Once")
    assert len(requests) == 1
    assert sleeps == []
