"""Hermetic tests for AnthropicManagedAgentsSession.

Every Anthropic SDK call is stubbed — no real network, no ngrok, no Flask
actually listening. The goal is to lock the control flow (ExitStack
resource ordering, event drain, envelope parsing, sha256 cross-check) and
all failure modes without any flakiness from external services.

Stub strategy:
  - _StubAnthropic: records every beta.*/environments/agents/sessions call,
    returns fake resources with preset ids.
  - _StubEventStream: a context manager yielding fake events; used to
    exercise the drain loop, timeout path, and stop-reason detection.
  - ngrok_tunnel and _spawn_upload_sink are monkeypatched away — we don't
    want real tunnel spawns in the test suite.

Coverage mirrors the three-class failure taxonomy:
  - SessionInfrastructureError paths (upload sink failure, stack create failure)
  - SessionProtocolError paths (sha256 mismatch, missing uploaded_sha256)
  - Timeout path (TimeoutError inside _drain_stream → failure dict, not raise)
  - Happy path (PASS envelope, sha256 verified, EditorSessionResult.ok)
  - Agent FAIL verdict passes through as EditorSessionResult without raising
  - Unparseable final text → EditorSessionResult with empty payload
  - Archive order: session archived before agent before environment (LIFO)
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from src.producer.editor import (
    EditorSessionResult,
    SessionInfrastructureError,
    SessionProtocolError,
    parse_editor_result,
)
from src.producer.editor_session import (
    AnthropicManagedAgentsSession,
    _SinkState,
    _safe_sha256_of,
)
from src.producer.upload_endpoint import generate_secret, mint_token


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


@dataclass
class _FakeResource:
    """Fake SDK resource object (env, agent, session)."""
    id: str
    status: str = "active"
    usage: dict | None = None


@dataclass
class _Call:
    method: str
    args: tuple
    kwargs: dict


class _StubBetaSessions:
    """Minimal sessions+events+resources stubs. Records all calls."""

    def __init__(self, session_id: str, events: list[Any]):
        self._session_id = session_id
        self._events_to_yield = events
        self.calls: list[_Call] = []
        self.resources = _StubResources()
        self.events = _StubEvents(session_id, events, self.calls)

    def create(self, **kw) -> _FakeResource:
        self.calls.append(_Call("sessions.create", (), kw))
        r = _FakeResource(id=self._session_id)
        r.usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        }
        return r

    def retrieve(self, session_id, **kw) -> _FakeResource:
        self.calls.append(_Call("sessions.retrieve", (session_id,), kw))
        r = _FakeResource(id=session_id)
        r.usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        }
        return r

    def archive(self, session_id, **kw) -> None:
        self.calls.append(_Call("sessions.archive", (session_id,), kw))


class _StubEvents:
    def __init__(self, session_id, events_to_yield, calls_list):
        self._session_id = session_id
        self._events = events_to_yield
        self._calls = calls_list

    @contextlib.contextmanager
    def stream(self, session_id, **kw) -> Iterator[list]:
        self._calls.append(_Call("events.stream", (session_id,), kw))
        yield iter(self._events)

    def send(self, session_id, events=None, **kw) -> None:
        self._calls.append(_Call("events.send", (session_id,), {"events": events, **kw}))


class _StubResources:
    def add(self, session_id, **kw) -> _FakeResource:
        return _FakeResource(id="res_fake")


class _FakeEvent:
    """Minimal event object matching the Anthropic SDK's event shape."""
    def __init__(self, type_: str, text: str | None = None):
        self.type = type_
        if text is not None:
            block = MagicMock()
            block.type = "text"
            block.text = text
            self.content = [block]
        else:
            self.content = []


def _idle_event(stop_reason: str = "end_turn") -> _FakeEvent:
    ev = _FakeEvent("session.status_idle")
    ev.stop_reason = stop_reason
    return ev


def _text_event(text: str) -> _FakeEvent:
    return _FakeEvent("agent.message", text=text)


def _build_stub_client(
    session_id: str = "sesn_test_abc",
    events_to_yield: list | None = None,
) -> tuple[MagicMock, _StubBetaSessions]:
    """Build a fake Anthropic client + return the sessions stub for assertions."""
    if events_to_yield is None:
        events_to_yield = [_idle_event()]

    sessions_stub = _StubBetaSessions(session_id, events_to_yield)

    client = MagicMock()
    client.beta.environments.create.return_value = _FakeResource(id="env_test")
    client.beta.environments.archive = MagicMock()
    client.beta.agents.create.return_value = _FakeResource(id="agt_test")
    client.beta.agents.archive = MagicMock()
    client.beta.sessions = sessions_stub

    return client, sessions_stub


def _make_session(
    storage_root: Path,
    client: Any,
    upload_port: int = 0,
) -> AnthropicManagedAgentsSession:
    return AnthropicManagedAgentsSession(
        client=client,
        storage_root=storage_root,
        upload_port=upload_port,
        startup_timeout_s=0.1,  # fast for tests
    )


_BASE_RUN_KWARGS = {
    "system_prompt": "STUB",
    "skills": ("hyperframes",),
    "model": "claude-opus-4-7",
    "apt_packages": ("ffmpeg",),
    "workspace_dir": Path("/tmp/ws"),
    "initial_message": "Assemble the film.",
    "timeout_s": 60.0,
}


# ---------------------------------------------------------------------------
# _safe_sha256_of helper
# ---------------------------------------------------------------------------


def test_safe_sha256_returns_hex_on_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"hello")
    assert _safe_sha256_of(f) == hashlib.sha256(b"hello").hexdigest()


def test_safe_sha256_returns_none_on_missing_file(tmp_path: Path) -> None:
    assert _safe_sha256_of(tmp_path / "nope.bin") is None


# ---------------------------------------------------------------------------
# Sink state rebind
# ---------------------------------------------------------------------------


def test_rebind_sink_changes_session_id_and_remints_token(tmp_path: Path) -> None:
    client, _ = _build_stub_client()
    session = _make_session(tmp_path, client)

    secret = generate_secret()
    orig_token = mint_token("pending_123", secret)
    sink = _SinkState(
        public_url="https://x.ngrok-free.app",
        token=orig_token,
        session_id="pending_123",
        storage_dir=tmp_path / "pending_123",
        sha256_of=lambda name: None,
    )
    sink.__dict__["_secret"] = secret

    rebound = session._rebind_sink_to_session(sink, "sesn_real_abc")
    assert rebound.session_id == "sesn_real_abc"
    assert rebound.token.session_id == "sesn_real_abc"
    # URL preserved
    assert rebound.public_url == sink.public_url


# ---------------------------------------------------------------------------
# Happy path (monkeypatched ngrok + Flask thread)
# ---------------------------------------------------------------------------


def _make_mock_sink(storage_root: Path, session_id: str = "sesn_test") -> _SinkState:
    """Pre-seeded sink with a real file for sha256 verification."""
    sid_dir = storage_root / session_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    tar_gz = sid_dir / "edit.tar.gz"
    tar_gz.write_bytes(b"fake-tar-gz-bytes")
    secret = generate_secret()
    token = mint_token(session_id, secret)
    state = _SinkState(
        public_url="https://probe.ngrok-free.app",
        token=token,
        session_id=session_id,
        storage_dir=sid_dir,
        sha256_of=lambda name: _safe_sha256_of(sid_dir / name) if (sid_dir / name).exists() else None,
    )
    state.__dict__["_secret"] = secret
    return state


EDITOR_RESULT_PASS = (
    'EDITOR_RESULT: {"verdict":"PASS",'
    '"composition_path":"artifacts/edit/index.html",'
    '"composition_archive_path":"artifacts/edit/composition.zip",'
    '"render_path":"artifacts/edit/out.mp4",'
    '"render_md5":"' + "a" * 32 + '",'
    '"duration_s":58.3,'
    '"renderer_version":"0.4.12",'
    '"uploaded_sha256":"PLACEHOLDER_SHA",'
    '"notes":"3 iterations"}'
)


def test_happy_path_run_returns_ok_session_result(
    tmp_path: Path, monkeypatch
) -> None:
    """Full happy-path run with monkeypatched ngrok + upload sink.
    The sha256 in the EDITOR_RESULT envelope matches the file seeded in
    the mock sink's storage_dir."""
    session_id = "sesn_happy"
    sink = _make_mock_sink(tmp_path, session_id)
    real_sha = _safe_sha256_of(sink.storage_dir / "edit.tar.gz")

    # Inject the real sha into the EDITOR_RESULT text so verification passes.
    result_text = EDITOR_RESULT_PASS.replace("PLACEHOLDER_SHA", real_sha)
    events = [_text_event(result_text), _idle_event()]

    client, sessions_stub = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    # Patch _spawn_upload_sink to yield our pre-seeded sink
    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    # Rebind is a no-op in this test (same session_id hint vs real)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    result = sess.run(**_BASE_RUN_KWARGS)

    assert isinstance(result, EditorSessionResult)
    assert result.verdict == "ok"
    assert result.final_payload["verdict"] == "PASS"
    assert result.final_payload["uploaded_sha256"] == real_sha
    # cost_usd is non-zero (usage had tokens)
    assert result.cost_usd > 0.0


# ---------------------------------------------------------------------------
# Infrastructure failures → SessionInfrastructureError
# ---------------------------------------------------------------------------


def test_upload_sink_failure_raises_infra_error(
    tmp_path: Path, monkeypatch
) -> None:
    client, _ = _build_stub_client()
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _broken_sink(hint=None):
        raise RuntimeError("ngrok refused to start")
        yield  # unreachable but makes it a generator

    monkeypatch.setattr(sess, "_spawn_upload_sink", _broken_sink)

    with pytest.raises(SessionInfrastructureError, match="ngrok refused"):
        sess.run(**_BASE_RUN_KWARGS)


def test_session_create_failure_raises_infra_error(
    tmp_path: Path, monkeypatch
) -> None:
    """If Anthropic sessions.create raises (e.g. 5xx), infra error."""
    session_id = "sesn_fail"
    sink = _make_mock_sink(tmp_path, session_id)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    def _create_stack_raises(**_kw):
        raise RuntimeError("Anthropic 500")

    client, _ = _build_stub_client()
    sess = _make_session(tmp_path, client)
    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)
    monkeypatch.setattr(
        sess, "_create_session_stack",
        lambda *, stack, **kw: (_ for _ in ()).throw(RuntimeError("Anthropic 500"))
    )

    with pytest.raises(SessionInfrastructureError, match="Anthropic 500"):
        sess.run(**_BASE_RUN_KWARGS)


# ---------------------------------------------------------------------------
# Protocol failures → SessionProtocolError
# ---------------------------------------------------------------------------


def test_sha256_mismatch_raises_protocol_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent reports a sha256 that doesn't match what the endpoint stored."""
    session_id = "sesn_mismatch"
    sink = _make_mock_sink(tmp_path, session_id)
    wrong_sha = "b" * 64
    result_text = EDITOR_RESULT_PASS.replace("PLACEHOLDER_SHA", wrong_sha)
    events = [_text_event(result_text), _idle_event()]

    client, _ = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    with pytest.raises(SessionProtocolError, match="mismatch"):
        sess.run(**_BASE_RUN_KWARGS)


def test_missing_uploaded_sha256_raises_protocol_error(
    tmp_path: Path, monkeypatch
) -> None:
    """PASS envelope without uploaded_sha256 → protocol error."""
    session_id = "sesn_nosha"
    sink = _make_mock_sink(tmp_path, session_id)
    result_text = (
        'EDITOR_RESULT: {"verdict":"PASS","composition_path":"x",'
        '"render_path":"y","render_md5":"' + "a" * 32 + '",'
        '"duration_s":10.0,"uploaded_sha256":""}'
    )
    events = [_text_event(result_text), _idle_event()]

    client, _ = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    with pytest.raises(SessionProtocolError, match="missing uploaded_sha256"):
        sess.run(**_BASE_RUN_KWARGS)


def test_no_artifact_in_storage_dir_raises_protocol_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent claims PASS + reports a sha256 but no file is in the sink."""
    session_id = "sesn_empty"
    # Build sink with no file seeded
    sink = _SinkState(
        public_url="https://x.ngrok",
        token=mint_token(session_id, generate_secret()),
        session_id=session_id,
        storage_dir=tmp_path / session_id,
        sha256_of=lambda name: None,  # nothing there
    )
    result_text = EDITOR_RESULT_PASS.replace("PLACEHOLDER_SHA", "a" * 64)
    events = [_text_event(result_text), _idle_event()]

    client, _ = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    with pytest.raises(SessionProtocolError, match="no artifact"):
        sess.run(**_BASE_RUN_KWARGS)


# ---------------------------------------------------------------------------
# Timeout → clean EditorSessionResult (not a raise)
# ---------------------------------------------------------------------------


def test_timeout_returns_failed_session_result_not_raise(
    tmp_path: Path, monkeypatch
) -> None:
    """Timeout inside the drain loop must return a failure result, NOT
    raise TimeoutError — the EditorTool maps it to failure_stage=timeout."""
    session_id = "sesn_timeout"
    sink = _make_mock_sink(tmp_path, session_id)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    # Patch _drain_stream to simulate timeout
    def _timeout_drain(*, session_id, initial_message, timeout_s):
        raise TimeoutError(f"session {session_id} did not idle within {timeout_s}s")

    client, _ = _build_stub_client(session_id=session_id)
    sess = _make_session(tmp_path, client)
    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)
    monkeypatch.setattr(sess, "_drain_stream", _timeout_drain)

    result = sess.run(**_BASE_RUN_KWARGS)

    assert result.verdict == "failed"
    assert result.failure_stage == "timeout"
    # Does not raise — caller handles this in the failure dict path


# ---------------------------------------------------------------------------
# Agent FAIL verdict → EditorSessionResult with payload (not a raise)
# ---------------------------------------------------------------------------


def test_agent_fail_verdict_passes_through_as_session_result(
    tmp_path: Path, monkeypatch
) -> None:
    """When the agent reports FAIL, the result carries the envelope
    without any additional raise. EditorTool classifies it as
    'agent_reported_fail' via _project_session_result."""
    session_id = "sesn_agfail"
    sink = _make_mock_sink(tmp_path, session_id)
    fail_text = (
        'EDITOR_RESULT: {"verdict":"FAIL","failed_at":"render",'
        '"stderr_tail":"codec mismatch","notes":"retry upstream"}'
    )
    events = [_text_event(fail_text), _idle_event()]

    client, _ = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    result = sess.run(**_BASE_RUN_KWARGS)

    assert result.verdict == "ok"   # session ran fine; agent chose FAIL
    assert result.final_payload["verdict"] == "FAIL"
    assert result.final_payload["failed_at"] == "render"


# ---------------------------------------------------------------------------
# Unparseable final text → empty payload (not a raise)
# ---------------------------------------------------------------------------


def test_unparseable_final_text_returns_empty_payload(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent ends without emitting EDITOR_RESULT. We return ok + empty
    payload so EditorTool maps it to 'unparseable_verdict'."""
    session_id = "sesn_noresult"
    sink = _make_mock_sink(tmp_path, session_id)
    events = [
        _text_event("I did my best but didn't finish in time."),
        _idle_event(),
    ]

    client, _ = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)

    result = sess.run(**_BASE_RUN_KWARGS)

    assert result.verdict == "ok"
    assert result.final_payload == {}


# ---------------------------------------------------------------------------
# Archive order — LIFO via ExitStack (session before agent before env)
# ---------------------------------------------------------------------------


def test_archive_called_for_all_three_resources(
    tmp_path: Path, monkeypatch
) -> None:
    """Every resource gets archived on clean exit. Order is session →
    agent → environment (LIFO, per ExitStack push order)."""
    session_id = "sesn_archive"
    sink = _make_mock_sink(tmp_path, session_id)
    real_sha = _safe_sha256_of(sink.storage_dir / "edit.tar.gz")
    result_text = EDITOR_RESULT_PASS.replace("PLACEHOLDER_SHA", real_sha)
    events = [_text_event(result_text), _idle_event()]

    client, sessions_stub = _build_stub_client(session_id=session_id, events_to_yield=events)
    sess = _make_session(tmp_path, client)

    archive_order: list[str] = []

    def _fake_archive(kind, rid):
        archive_order.append(kind)

    @contextlib.contextmanager
    def _fake_sink(hint=None):
        yield sink

    monkeypatch.setattr(sess, "_spawn_upload_sink", _fake_sink)
    monkeypatch.setattr(sess, "_rebind_sink_to_session", lambda s, sid: s)
    monkeypatch.setattr(sess, "_archive_safely", _fake_archive)

    sess.run(**_BASE_RUN_KWARGS)

    # LIFO: session was pushed last, archived first
    assert archive_order == ["session", "agent", "environment"]


# ---------------------------------------------------------------------------
# Kickoff message contains upload block
# ---------------------------------------------------------------------------


def test_full_kickoff_contains_upload_url_and_token(tmp_path: Path) -> None:
    """_build_full_kickoff appends the upload block to the base message."""
    secret = generate_secret()
    token = mint_token("sesn_x", secret)
    sink = _SinkState(
        public_url="https://test.ngrok-free.app",
        token=token,
        session_id="sesn_x",
        storage_dir=tmp_path / "sesn_x",
        sha256_of=lambda name: None,
    )
    client, _ = _build_stub_client()
    sess = _make_session(tmp_path, client)

    full = sess._build_full_kickoff(base_message="BASE", sink=sink)
    assert "https://test.ngrok-free.app/upload/sesn_x" in full
    assert token.encode() in full
    assert "curl" in full
    assert "UPLOAD_TOKEN" in full
