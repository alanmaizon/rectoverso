"""Smoke tests for src.rectoverso.cli.

Intent:     each subcommand exits with the documented code and emits parseable JSON
Approach:   drive main() directly with argv; capture stdout via capsys; parse JSON
Fixtures:   builds minimal manifests on disk under tmp_path so tests are hermetic
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.rectoverso.cli import main
from tests.producer.conftest import (
    add_minimal_shot,
    minimal_manifest,
)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def mpath(tmp_path: Path) -> Path:
    """A minimal valid manifest on disk."""
    m = minimal_manifest()
    add_minimal_shot(m, "sh_001")
    return _write(tmp_path / "manifest.json", m)


# --- version --------------------------------------------------------------


def test_version_exits_zero(capsys) -> None:
    assert main(["version"]) == 0
    assert "rectoverso" in capsys.readouterr().out


# --- manifest -------------------------------------------------------------


def test_manifest_show_json(mpath: Path, capsys) -> None:
    assert main(["manifest", "show", "--json", str(mpath)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] == "proj_test"
    assert payload["shot_count"] == 1
    assert payload["shots_by_status"] == {"created": 1}


def test_manifest_show_pretty(mpath: Path, capsys) -> None:
    assert main(["manifest", "show", str(mpath)]) == 0
    out = capsys.readouterr().out
    assert "project_id" in out
    assert "budget" in out


def test_manifest_show_missing_file(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["manifest", "show", str(tmp_path / "nope.json")])
    assert excinfo.value.code == 2
    assert "not found" in capsys.readouterr().err


def test_manifest_validate_ok(mpath: Path, capsys) -> None:
    assert main(["manifest", "validate", "--json", str(mpath)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_manifest_validate_invalid_schema(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    del m["brief"]["logline"]  # required field
    bad = _write(tmp_path / "bad.json", m)
    with pytest.raises(SystemExit) as excinfo:
        main(["manifest", "validate", str(bad)])
    assert excinfo.value.code == 3
    assert "schema validation failed" in capsys.readouterr().err


def test_manifest_show_dirty_flag(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    m["run_state"]["resumable"] = False
    path = _write(tmp_path / "dirty.json", m)
    assert main(["manifest", "show", str(path)]) == 1  # documented non-zero on dirty


# --- manifest migrate-providers -----------------------------------------


def test_migrate_providers_dry_run_reports_changes(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    # Stale IDs — provider exists in capabilities.yaml but chosen_model doesn't
    # match the canonical value.
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan-2.7-plus"  # invalid — was never real
    path = _write(tmp_path / "stale.json", m)

    assert main([
        "manifest", "migrate-providers",
        "--capabilities", "router/capabilities.yaml",
        "--dry-run", "--json", str(path),
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert len(payload["changed"]) == 1
    c = payload["changed"][0]
    assert c["shot_id"] == "sh_001"
    assert c["from"] == "wan-2.7-plus"
    assert c["to"] == "wan2.7-t2v"

    # Dry-run must not mutate on disk.
    on_disk = json.loads(path.read_text())
    assert on_disk["shots"][0]["routing"]["chosen_model"] == "wan-2.7-plus"


def test_migrate_providers_writes_manifest(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["routing"]["chosen_provider"] = "fal_kling_2_1_pro"
    shot["routing"]["chosen_model"] = "kling-video/v2.1/pro/image-to-video"  # missing fal-ai/ prefix
    path = _write(tmp_path / "stale.json", m)

    assert main([
        "manifest", "migrate-providers",
        "--capabilities", "router/capabilities.yaml",
        "--json", str(path),
    ]) == 0

    on_disk = json.loads(path.read_text())
    routing = on_disk["shots"][0]["routing"]
    assert routing["chosen_model"] == "fal-ai/kling-video/v2.1/pro/image-to-video"
    # history should carry an audit row
    events = [h["event"] for h in on_disk["shots"][0]["history"]]
    assert "model_id_migrated" in events


def test_migrate_providers_is_idempotent(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    # Already canonical — second run should report zero changes.
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"
    path = _write(tmp_path / "clean.json", m)

    main(["manifest", "migrate-providers", "--capabilities", "router/capabilities.yaml",
          "--json", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == []


def test_migrate_providers_ignores_unknown_provider(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["routing"]["chosen_provider"] = "future_provider_we_dont_know"
    shot["routing"]["chosen_model"] = "anything"
    path = _write(tmp_path / "unknown.json", m)

    main(["manifest", "migrate-providers", "--capabilities", "router/capabilities.yaml",
          "--json", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == []


# --- budget ---------------------------------------------------------------


def test_budget_show(mpath: Path, capsys) -> None:
    assert main(["budget", "show", "--json", str(mpath)]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["cap_usd"] == 151.0


def test_budget_check_allow(mpath: Path, capsys) -> None:
    code = main([
        "budget", "check",
        "--provider", "fal_kling_2_1_pro",
        "--cost", "1.50",
        "--json",
        str(mpath),
    ])
    assert code == 0
    check = json.loads(capsys.readouterr().out)
    assert check["allowed"] is True
    assert check["projected_spent_usd"] == 1.50


def test_budget_check_refuse_hard_cap(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    m["budget"]["spent_usd"] = 150.50
    path = _write(tmp_path / "m.json", m)
    code = main([
        "budget", "check",
        "--provider", "fal_kling_2_1_pro",
        "--cost", "1.0",
        "--json",
        str(path),
    ])
    assert code == 1
    check = json.loads(capsys.readouterr().out)
    assert check["allowed"] is False
    assert "cap breached" in check["rationale"]


def test_budget_check_veo_sub_cap(tmp_path: Path, capsys) -> None:
    m = minimal_manifest()
    m["budget"]["by_provider"] = {"vertex_veo_3_1_fast": 14.50}
    m["budget"]["spent_usd"] = 14.50
    path = _write(tmp_path / "m.json", m)
    code = main([
        "budget", "check",
        "--provider", "vertex_veo_3_1_fast",
        "--cost", "1.00",
        "--json",
        str(path),
    ])
    assert code == 1
    assert "Veo project cap" in json.loads(capsys.readouterr().out)["rationale"]


# --- events ---------------------------------------------------------------


def test_events_tail_missing_db(tmp_path: Path, capsys) -> None:
    code = main(["events", "tail", "--db", str(tmp_path / "nope.db")])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_events_tail_empty_db(tmp_path: Path, capsys) -> None:
    from src.producer import open_event_log

    db = tmp_path / "events.db"
    with open_event_log(db):
        pass  # create the schema; no events
    assert main(["events", "tail", "--json", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_events_tail_with_events(tmp_path: Path, capsys) -> None:
    from src.producer import open_event_log

    db = tmp_path / "events.db"
    with open_event_log(db) as log:
        a = log.write("dispatch_intent", agent="shot_judge", shot_id="sh_001")
        log.write("dispatch_result", agent="shot_judge", shot_id="sh_001", ref_event_id=a)

    assert main(["events", "tail", "--json", "--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert payload[0]["kind"] == "dispatch_intent"


def test_events_tail_shot_filter(tmp_path: Path, capsys) -> None:
    from src.producer import open_event_log

    db = tmp_path / "events.db"
    with open_event_log(db) as log:
        log.write("dispatch_intent", shot_id="sh_001")
        log.write("dispatch_intent", shot_id="sh_002")
    assert main(["events", "tail", "--shot", "sh_002", "--json", "--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["shot_id"] for e in payload] == ["sh_002"]


# --- router ---------------------------------------------------------------


def test_router_pick_ok(mpath: Path, capsys) -> None:
    code = main([
        "router", "pick",
        "--shot", "sh_001",
        "--json",
        str(mpath),
    ])
    # sh_001 in the minimal fixture has no humans, not hero, low motion → Wan
    assert code == 0
    choice = json.loads(capsys.readouterr().out)
    assert choice["provider_id"].startswith("alibaba_wan")


def test_router_pick_unknown_shot(mpath: Path, capsys) -> None:
    assert main(["router", "pick", "--shot", "sh_999", str(mpath)]) == 2


# --- contracts ------------------------------------------------------------


def test_contracts_verify_allow_film_level(mpath: Path, capsys) -> None:
    code = main(["contracts", "verify", "--agent", "editor_agent", "--json", str(mpath)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "allow"


def test_contracts_verify_block_revision_without_attempts(mpath: Path, capsys) -> None:
    code = main([
        "contracts", "verify",
        "--agent", "prompt_smith",
        "--shot", "sh_001",
        "--revision",
        "--json",
        str(mpath),
    ])
    assert code == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "block"
    assert any("shot_judge_to_prompt_smith" in v["contract"] for v in payload["violations"])


def test_contracts_verify_rejects_unknown_agent(mpath: Path, capsys) -> None:
    # argparse rejects with exit 2 before our code sees it
    with pytest.raises(SystemExit) as excinfo:
        main(["contracts", "verify", "--agent", "not_an_agent", str(mpath)])
    assert excinfo.value.code == 2
