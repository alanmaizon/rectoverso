"""Shared HTTP test plumbing for adapter tests.

Five Tier-4 adapter test modules need the same handful of helpers to inject
fake urlopen + response bodies. Per-file copies drifted slightly over time
(different calls[] signatures, different fake-response eof semantics); this
module is the canonical single source.

Exports:
    FakeResponse           — context-manager wrapper over a fixed byte body
    json_response(obj)     — FakeResponse with JSON-encoded body
    http_error(code, body) — urllib.error.HTTPError with a readable fp
    make_urlopen(plan)     — consumes `plan` in order, records a `.calls` log

Adapter tests drive the fake like this:

    plan = [
        lambda req: json_response({"output": {"task_id": "x"}}),
        lambda req: json_response({"output": {"task_status": "SUCCEEDED"}}),
        lambda req: FakeResponse(b"<mp4-bytes>"),
    ]
    fake = make_urlopen(plan)
    tool = WanRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool("sh_001", payload)
    assert fake.calls[0]["method"] == "POST"
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Callable


class FakeResponse:
    """Context-manager + .read(n) mock matching urlopen's return shape.

    Used as a stand-in for the object returned by urllib.request.urlopen.
    Handles both bulk `.read()` (for JSON responses) and chunked `.read(n)`
    reads (for video/image downloads that stream in 64KB chunks).
    """

    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            return self._body
        out, self._body = self._body[:n], self._body[n:]
        return out


def json_response(obj: Any) -> FakeResponse:
    """Wrap a JSON-serializable object in a FakeResponse."""
    return FakeResponse(json.dumps(obj).encode("utf-8"))


def http_error(
    status: int, body: str, *, url: str = "https://fake.local/"
) -> urllib.error.HTTPError:
    """Build an HTTPError whose `.read()` returns `body`.

    Adapters call `exc.read()` on these to surface the upstream error body
    in their failure payloads — the real urllib HTTPError stores it on `.fp`
    and only reads lazily, so we mirror that with an io.BytesIO.
    """
    return urllib.error.HTTPError(
        url=url,
        code=status,
        msg="Fake",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


def make_urlopen(
    plan: list[Callable[[Any], FakeResponse]],
) -> Callable[..., FakeResponse]:
    """Build a fake urlopen that consumes `plan` in FIFO order.

    Each entry is a callable that receives the `req_or_url` argument urllib
    was called with and returns a FakeResponse. The returned function has
    a `.calls` list where each entry is `{url, method, headers, body}`:

        - `body` is the JSON-decoded request body when Content-Type-ish
        - None when the request had no body or the body wasn't valid JSON

    Raises AssertionError if called more times than the plan has entries —
    that's almost always a test bug, so failing loud beats a silent hang.
    """
    calls: list[dict[str, Any]] = []

    def _fake(req_or_url: Any, timeout: float | None = None) -> FakeResponse:
        if not plan:
            raise AssertionError("unexpected extra urlopen call")
        url = getattr(req_or_url, "full_url", None) or str(req_or_url)
        method = getattr(req_or_url, "method", "GET")
        headers = dict(getattr(req_or_url, "headers", {}) or {})
        data = getattr(req_or_url, "data", None)
        parsed_body: Any = None
        if data:
            try:
                parsed_body = json.loads(data.decode("utf-8"))
            except (ValueError, TypeError, UnicodeDecodeError):
                parsed_body = None
        calls.append({
            "url": url,
            "method": method,
            "headers": headers,
            "body": parsed_body,
        })
        return plan.pop(0)(req_or_url)

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake
