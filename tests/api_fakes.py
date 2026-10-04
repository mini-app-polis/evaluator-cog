"""Fakes for tests that talk to the Kaiano API through the shared client.

evaluator-cog calls the API through ``KaianoApiClient``'s typed methods, which
build the request from the contract model and validate the answer against the
response model. These fakes replace only the HTTP call — ``post`` and ``get``
— so that path runs as it does in production, and what a fake returns has to
be what the API really returns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

# From the defining module rather than the package, so a test that swaps
# ``mini_app_polis.api`` out of sys.modules cannot hand this a fake.
from mini_app_polis.api.client import KaianoApiClient

_META = {"count": 1, "total": 1, "version": "test"}

#: Every field of a stored evaluation row, as the API returns it.
_ROW: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-000000000001",
    "run_id": None,
    "violation_id": None,
    "repo": "test-repo",
    "dimension": "pipeline_consistency",
    "severity": "WARN",
    "finding": "a finding",
    "suggestion": None,
    "standards_version": None,
    "evaluator_version": None,
    "source": None,
    "flow_name": None,
    "evaluated_at": "2026-09-01T00:00:00Z",
}


def api(
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
) -> KaianoApiClient:
    """A real client whose HTTP calls are ``post`` and ``get``.

    ``post`` defaults to storing whatever it is sent; ``get`` to no rows.
    Either may be a function or a ``MagicMock``.
    """
    client = KaianoApiClient(base_url="https://example.test", api_key="k")
    client.post = post if post is not None else MagicMock(side_effect=store)  # type: ignore[method-assign]
    client.get = get if get is not None else MagicMock(return_value=rows())  # type: ignore[method-assign]
    return client


def row(**fields: Any) -> dict[str, Any]:
    """One evaluation row as the API returns it, with ``fields`` set."""
    return {**_ROW, **fields}


def rows(*items: dict[str, Any]) -> dict[str, Any]:
    """A GET /v1/evaluations answer holding ``items``."""
    return {"data": list(items), "meta": {**_META, "count": len(items)}}


def store(
    _path: str, payload: dict[str, Any], *, deduplicated: bool = False
) -> dict[str, Any]:
    """A POST /v1/evaluations answer: the row that ``payload`` became."""
    return {"data": {**row(**payload), "deduplicated": deduplicated}, "meta": _META}
