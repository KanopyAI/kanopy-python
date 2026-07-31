"""Exceptions raised by the Kanopy SDK."""

from __future__ import annotations

from typing import Any

import httpx


class KanopyError(Exception):
    """A non-successful response from the Kanopy API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        detail: Any = None,
        request_id: str | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.request_id = request_id
        self.response = response

    @classmethod
    def from_response(cls, response: httpx.Response) -> KanopyError:
        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass

        message = (
            payload.get("error") or payload.get("message") or response.reason_phrase
        )
        return cls(
            str(message or f"Kanopy API returned HTTP {response.status_code}"),
            status_code=response.status_code,
            code=payload.get("code"),
            detail=payload.get("detail"),
            request_id=payload.get("request_id")
            or response.headers.get("X-Request-ID"),
            response=response,
        )


class KanopyUploadError(RuntimeError):
    """A direct-to-storage upload part could not be transferred."""

    def __init__(
        self, message: str, *, part_number: int, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.part_number = part_number
        self.status_code = status_code
