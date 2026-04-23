"""Tests for compute_opus_47_cost — the Managed Agents session cost helper.

Shape was probe-confirmed 2026-04-23 (see research/managed_agents_platform_api.md
§ Day 5 probe findings). The probe itself produced real numbers:

    input_tokens             = 12
    output_tokens            = 933
    cache_read_input_tokens  = 54049
    cache_creation           = {ephemeral_5m_input_tokens: 9966, ephemeral_1h: 0}

Expected cost: ~$0.338 (validates formula to $0.001).

Key invariants these tests lock:
    - `cache_creation` is a NESTED DICT, not a flat int
    - Field naming is asymmetric (`cache_creation` vs `cache_read_input_tokens`)
    - Helper is defensive — unknown/missing fields count as zero, never raises
    - Accepts dict, Pydantic v2 model_dump, Pydantic v1 dict, plain object
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.producer._common import compute_opus_47_cost


# ---------------------------------------------------------------------------
# Probe-confirmed shape — the canonical dict
# ---------------------------------------------------------------------------


def _probe_usage() -> dict:
    """Exact usage dict from scratch/managed_agents_editor_probe/report.json.
    If the SDK ever breaks this shape, this test file is the canary."""
    return {
        "input_tokens": 12,
        "output_tokens": 933,
        "cache_read_input_tokens": 54049,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 9966,
            "ephemeral_1h_input_tokens": 0,
        },
    }


def test_probe_shape_reproduces_observed_cost() -> None:
    """Formula must reproduce the $0.34 session cost observed from the
    2026-04-23 probe. Locks the per-MTok prices + the cache_creation
    nesting against drift."""
    cost = compute_opus_47_cost(_probe_usage())
    # Hand computation:
    #   input:       12     * 15.00 / 1e6 = 0.00018
    #   output:      933    * 75.00 / 1e6 = 0.06998 (0.069975)
    #   cache_read:  54049  *  1.50 / 1e6 = 0.08107 (0.0810735)
    #   cache_5m:    9966   * 18.75 / 1e6 = 0.18686 (0.1868625)
    #   cache_1h:    0      * 30.00 / 1e6 = 0
    # Total ≈ 0.3381
    assert cost == pytest.approx(0.3381, abs=0.0001)


def test_every_field_contributes() -> None:
    """Each price tier accrues independently; zero tokens means zero contrib."""
    only_input = {"input_tokens": 1_000_000}
    assert compute_opus_47_cost(only_input) == pytest.approx(15.00, abs=0.01)

    only_output = {"output_tokens": 1_000_000}
    assert compute_opus_47_cost(only_output) == pytest.approx(75.00, abs=0.01)

    only_cache_read = {"cache_read_input_tokens": 1_000_000}
    assert compute_opus_47_cost(only_cache_read) == pytest.approx(1.50, abs=0.01)

    only_5m = {"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000}}
    assert compute_opus_47_cost(only_5m) == pytest.approx(18.75, abs=0.01)

    only_1h = {"cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
    assert compute_opus_47_cost(only_1h) == pytest.approx(30.00, abs=0.01)


# ---------------------------------------------------------------------------
# Defensive-programming invariants
# ---------------------------------------------------------------------------


def test_none_usage_returns_zero() -> None:
    """Callers that never received a session (pre-session failure) pass
    None; cost is zero, not a raise."""
    assert compute_opus_47_cost(None) == 0.0


def test_empty_dict_returns_zero() -> None:
    assert compute_opus_47_cost({}) == 0.0


def test_missing_fields_count_as_zero() -> None:
    """Partial usage dicts (old SDK versions, pre-cache manifests) return
    cost on the fields that ARE present, zero-fill the rest."""
    partial = {"input_tokens": 1_000, "output_tokens": 500}
    expected = (1_000 * 15.00 + 500 * 75.00) / 1_000_000
    assert compute_opus_47_cost(partial) == pytest.approx(expected, abs=0.0001)


def test_cache_creation_as_non_dict_returns_zero_cache_cost() -> None:
    """If cache_creation is an int (collapsed from the nested structure),
    we treat it as unknown shape and zero-fill rather than misattribute."""
    usage = {"input_tokens": 100, "cache_creation": 9999}
    # Should still count input_tokens; cache_creation is unreadable
    assert compute_opus_47_cost(usage) == pytest.approx(
        100 * 15.00 / 1_000_000, abs=0.0001
    )


def test_string_values_coerce_safely() -> None:
    """Some JSON sources surface ints as strings. int() coercion handles it;
    actually-bad values (None, missing) fall through to 0."""
    usage = {"input_tokens": "1000", "output_tokens": None}
    assert compute_opus_47_cost(usage) == pytest.approx(0.015, abs=0.0001)


# ---------------------------------------------------------------------------
# SDK object coercion (dict | model_dump | dict() | to_dict | vars)
# ---------------------------------------------------------------------------


@dataclass
class _PydanticV2Like:
    """Stands in for a Pydantic v2 model with model_dump()."""
    data: dict[str, Any]

    def model_dump(self) -> dict:
        return self.data


@dataclass
class _PydanticV1Like:
    """Stands in for a Pydantic v1 model with .dict()."""
    data: dict[str, Any]

    def dict(self) -> dict:
        return self.data


@dataclass
class _SDKConventionLike:
    """Stands in for an Anthropic SDK helper with to_dict()."""
    data: dict[str, Any]

    def to_dict(self) -> dict:
        return self.data


class _PlainObject:
    """Stands in for a dataclass-less object with instance attrs only."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_pydantic_v2_object_is_coerced() -> None:
    usage = _PydanticV2Like(data=_probe_usage())
    assert compute_opus_47_cost(usage) == pytest.approx(0.3381, abs=0.0001)


def test_pydantic_v1_object_is_coerced() -> None:
    usage = _PydanticV1Like(data=_probe_usage())
    assert compute_opus_47_cost(usage) == pytest.approx(0.3381, abs=0.0001)


def test_to_dict_method_is_used() -> None:
    usage = _SDKConventionLike(data=_probe_usage())
    assert compute_opus_47_cost(usage) == pytest.approx(0.3381, abs=0.0001)


def test_plain_object_via_vars() -> None:
    """Fallback for SDK shapes we haven't enumerated — should still work via
    vars(). Nested dict stays a dict (we don't recurse into custom types)."""
    usage = _PlainObject(
        input_tokens=12,
        output_tokens=933,
        cache_read_input_tokens=54049,
        cache_creation={
            "ephemeral_5m_input_tokens": 9966,
            "ephemeral_1h_input_tokens": 0,
        },
    )
    assert compute_opus_47_cost(usage) == pytest.approx(0.3381, abs=0.0001)


def test_object_with_no_method_and_no_vars_returns_zero() -> None:
    """Slotted objects with no dict/vars accessor. Defensive — don't crash."""
    class _Slotted:
        __slots__ = ("hidden",)

        def __init__(self):
            self.hidden = "inaccessible"

    assert compute_opus_47_cost(_Slotted()) == 0.0
