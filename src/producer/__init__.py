"""Producer runtime — minimal orchestration shell.

Public API:

    from src.producer import (
        dispatch, DispatchResult, DispatchFailure, Tool,
        EventLog, open_event_log,
        load_manifest, save_manifest_atomic, ManifestValidationError, LoadResult,
    )

Usage sketch:

    events = EventLog("state/events.db")
    load = load_manifest("state/manifest.json")
    if load.was_dirty:
        raise RuntimeError("manifest requires reconciliation")
    manifest = load.manifest

    result = dispatch(
        agent="shot_judge",
        shot_id="sh_003",
        manifest=manifest,
        ctx={},
        tool=my_shot_judge_adapter,  # Tool Protocol
        events=events,
    )

    # Caller projects result.result into the manifest, then saves atomically:
    apply_judge_result(manifest, "sh_003", result.result)
    save_manifest_atomic("state/manifest.json", manifest, last_event_id=result.result_event_id)

The runtime is deliberately thin — the orchestration loop, retry policy, and
escalation logic live higher up (either in a Managed Agents session or in a
CLI script). This package provides the atoms those layers compose.
"""

from __future__ import annotations

from .dispatch import dispatch
from .events import EventLog, Event, KINDS, open_event_log
from .hyperframes import HyperframesTool
from .manifest_io import (
    LoadResult,
    ManifestValidationError,
    load_manifest,
    load_schema,
    save_manifest_atomic,
    validate_manifest,
)
from .types import DispatchFailure, DispatchResult, Tool

__all__ = [
    "dispatch",
    "DispatchFailure",
    "DispatchResult",
    "Tool",
    "EventLog",
    "Event",
    "KINDS",
    "open_event_log",
    "HyperframesTool",
    "LoadResult",
    "ManifestValidationError",
    "load_manifest",
    "load_schema",
    "save_manifest_atomic",
    "validate_manifest",
]
