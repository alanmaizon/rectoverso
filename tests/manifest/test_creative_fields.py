"""Schema tests for the Day-2 creative-pipeline fields.

Validates:
- `brief.artistic_style` + `brief.allow_artistic_experiments` (new optional anchors).
- `shots[].creative_feedback[]` (append-only agent-to-producer suggestions).
- `shots[].artistic_direction` (Producer-written style guidance for PromptSmith).
- `shots[].budget_decision` (shot-level creative pivot under budget pressure).
- Top-level `creative_decisions[]` (film-level reorders/merges/scope changes).

Each test constructs a deepcopy of the minimal manifest, applies one targeted
change, and checks the validator response. Failure messages reference the field
under test so regressions are easy to localize.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# brief.artistic_style / brief.allow_artistic_experiments
# ---------------------------------------------------------------------------


def test_brief_artistic_style_is_optional(validator, minimal_manifest):
    assert validator.is_valid(minimal_manifest), "baseline without artistic_style must validate"


def test_brief_artistic_style_accepts_string(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["brief"]["artistic_style"] = "film noir, low-key lighting, handheld"
    assert validator.is_valid(m)


def test_brief_artistic_style_rejects_empty_string(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["brief"]["artistic_style"] = ""
    assert not validator.is_valid(m), "empty artistic_style must fail (minLength=1)"


def test_brief_allow_artistic_experiments_accepts_boolean(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["brief"]["allow_artistic_experiments"] = True
    assert validator.is_valid(m)


def test_brief_rejects_unknown_field(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["brief"]["mystery_field"] = "no"
    assert not validator.is_valid(m), "additionalProperties must stay closed on brief"


# ---------------------------------------------------------------------------
# shots[].creative_feedback[]
# ---------------------------------------------------------------------------


def test_creative_feedback_accepts_creative_director_entry(
    validator, minimal_manifest, make_shot, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    shot = make_shot("sh_001")
    shot["creative_feedback"] = [
        {
            "ts": _ts(),
            "from_agent": "creative_director",
            "feedback": "sh_004 after sh_003 stalls the midpoint",
            "suggestion": "swap sh_004 and sh_005 to keep momentum",
            "priority": "high",
        }
    ]
    m["shots"].append(shot)
    assert validator.is_valid(m)


def test_creative_feedback_rejects_unknown_agent(
    validator, minimal_manifest, make_shot, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    shot = make_shot("sh_001")
    shot["creative_feedback"] = [
        {
            "ts": _ts(),
            "from_agent": "screenwriter",  # not in enum
            "feedback": "x",
        }
    ]
    m["shots"].append(shot)
    assert not validator.is_valid(m), "from_agent enum must exclude non-collaborators"


def test_creative_feedback_rejects_unknown_priority(
    validator, minimal_manifest, make_shot, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    shot = make_shot("sh_001")
    shot["creative_feedback"] = [
        {
            "ts": _ts(),
            "from_agent": "editor_agent",
            "feedback": "pacing sag",
            "priority": "urgent",  # not in enum
        }
    ]
    m["shots"].append(shot)
    assert not validator.is_valid(m)


def test_creative_feedback_addressed_tracking(
    validator, minimal_manifest, make_shot, deepcopy_
):
    """Producer marks an entry addressed. Must still validate."""
    m = deepcopy_(minimal_manifest)
    shot = make_shot("sh_001")
    shot["creative_feedback"] = [
        {
            "ts": _ts(),
            "from_agent": "audio_agent",
            "feedback": "dialogue overruns shot",
            "suggestion": "extend sh_001 by 0.3s",
            "priority": "medium",
            "addressed": True,
            "addressed_by": "extended duration from 3.0s to 3.3s",
            "addressed_at": _ts(),
        }
    ]
    m["shots"].append(shot)
    assert validator.is_valid(m)


# ---------------------------------------------------------------------------
# shots[].artistic_direction
# ---------------------------------------------------------------------------


def test_artistic_direction_accepts_freeform_string(
    validator, minimal_manifest, make_shot, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    shot = make_shot(
        "sh_001",
        artistic_direction="slow, deliberate handheld, breathing room between movements",
    )
    m["shots"].append(shot)
    assert validator.is_valid(m)


# ---------------------------------------------------------------------------
# shots[].budget_decision (shot-level)
# ---------------------------------------------------------------------------


def test_shot_budget_decision_accepts_full_entry(
    validator, minimal_manifest, make_shot, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    shot = make_shot(
        "sh_007",
        is_hero=True,
        budget_decision={
            "trigger": "Veo budget at 92% of $15 cap",
            "original_plan": "Veo hero render",
            "creative_pivot": "Kling render framed as 'grainier, more intimate' per artistic_direction",
            "rationale": "Preserve specialty budget for sh_010's required horizon shot",
            "decided_at": _ts(),
            "decided_by": "producer",
        },
    )
    m["shots"].append(shot)
    assert validator.is_valid(m)


# ---------------------------------------------------------------------------
# audio.dialogue[].compressibility_s (Audio ↔ Editor contract)
# ---------------------------------------------------------------------------


def test_dialogue_compressibility_is_optional(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["audio"]["dialogue"].append(
        {
            "shot_id": "sh_001",
            "line_id": "d_001_01",
            "text": "line",
            "voice_id": "v1",
            "audio_path": "artifacts/audio/d_001_01.wav",
            "duration_s": 2.1,
            "timing": {"in_s": 0.4, "out_s": 2.5},
        }
    )
    assert validator.is_valid(m), "baseline dialogue entry (no compressibility) must validate"


def test_dialogue_compressibility_accepts_zero(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["audio"]["dialogue"].append(
        {
            "shot_id": "sh_001",
            "line_id": "d_001_01",
            "text": "line",
            "voice_id": "v1",
            "audio_path": "artifacts/audio/d_001_01.wav",
            "duration_s": 2.1,
            "timing": {"in_s": 0.4, "out_s": 2.5},
            "compressibility_s": 0.0,
        }
    )
    assert validator.is_valid(m), "0.0 means already at min pace — must be valid"


def test_dialogue_compressibility_rejects_negative(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["audio"]["dialogue"].append(
        {
            "shot_id": "sh_001",
            "line_id": "d_001_01",
            "text": "line",
            "voice_id": "v1",
            "audio_path": "artifacts/audio/d_001_01.wav",
            "duration_s": 2.1,
            "timing": {"in_s": 0.4, "out_s": 2.5},
            "compressibility_s": -0.2,
        }
    )
    assert not validator.is_valid(m), "compressibility must be non-negative"


# ---------------------------------------------------------------------------
# top-level creative_decisions[]
# ---------------------------------------------------------------------------


def test_creative_decisions_required_as_empty_array(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    del m["creative_decisions"]
    assert not validator.is_valid(m), "creative_decisions is required (empty array is the default)"


def test_creative_decisions_accepts_merge_entry(validator, minimal_manifest, deepcopy_):
    m = deepcopy_(minimal_manifest)
    m["creative_decisions"] = [
        {
            "ts": _ts(),
            "decision_type": "merge",
            "trigger": "USD $120 spent with 3 shots remaining; Creative Director flagged rushed pacing",
            "action": "merged sh_008 + sh_009 into sh_008 at 6.5s; removed sh_009",
            "affected_shots": ["sh_008", "sh_009"],
            "rationale": "One held moment serves coastal-noir tone better than two 3s cuts under budget pressure.",
            "decided_by": "producer",
            "source_feedback_refs": [{"shot_id": "sh_008", "feedback_index": 2}],
        }
    ]
    assert validator.is_valid(m)


def test_creative_decisions_rejects_unknown_decision_type(
    validator, minimal_manifest, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    m["creative_decisions"] = [
        {
            "ts": _ts(),
            "decision_type": "vibe_shift",  # not in enum
            "trigger": "x",
            "action": "x",
            "rationale": "x",
            "decided_by": "producer",
        }
    ]
    assert not validator.is_valid(m)


def test_creative_decisions_rejects_non_producer_non_user_author(
    validator, minimal_manifest, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    m["creative_decisions"] = [
        {
            "ts": _ts(),
            "decision_type": "reorder",
            "trigger": "x",
            "action": "x",
            "rationale": "x",
            "decided_by": "creative_director",  # specialists don't write here
        }
    ]
    assert not validator.is_valid(m), "only producer/user may author creative_decisions"


def test_source_feedback_refs_require_shot_id_and_index(
    validator, minimal_manifest, deepcopy_
):
    m = deepcopy_(minimal_manifest)
    m["creative_decisions"] = [
        {
            "ts": _ts(),
            "decision_type": "reorder",
            "trigger": "x",
            "action": "swap 4 and 5",
            "rationale": "x",
            "decided_by": "producer",
            "source_feedback_refs": [{"shot_id": "sh_004"}],  # missing feedback_index
        }
    ]
    assert not validator.is_valid(m)
