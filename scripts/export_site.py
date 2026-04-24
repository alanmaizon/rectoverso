#!/usr/bin/env python3
"""Export pipeline outputs to site/data/ and site/media/ for the static site.

Usage:
    python scripts/export_site.py [--manifest state/manifest.json]
                                  [--events-db state/events.db]
                                  [--site-dir site]

Copies manifest.json + events.json (exported from events.db) into site/data/,
then copies the final MP4 (manifest.edit.render_path) into site/media/ if
one is present. The site's index.html reads from site/data/ when served.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("state/manifest.json"))
    parser.add_argument("--events-db", type=Path, default=Path("state/events.db"))
    parser.add_argument("--site-dir", type=Path, default=Path("site"))
    args = parser.parse_args()

    site_data = args.site_dir / "data"
    site_media = args.site_dir / "media"
    site_data.mkdir(parents=True, exist_ok=True)
    site_media.mkdir(parents=True, exist_ok=True)

    if not args.manifest.exists():
        print(f"error: manifest not found at {args.manifest}", file=sys.stderr)
        return 2

    manifest = json.loads(args.manifest.read_text())

    # 1. Copy manifest.
    dest_manifest = site_data / "manifest.json"
    shutil.copy2(args.manifest, dest_manifest)
    print(f"manifest  → {dest_manifest}")

    # 2. Export events DB → JSON.
    dest_events = site_data / "events.json"
    if args.events_db.exists():
        rows = _export_events(args.events_db)
        dest_events.write_text(json.dumps(rows, indent=2))
        print(f"events    → {dest_events} ({len(rows)} rows)")
    else:
        print(f"events-db not found at {args.events_db}; skipping events export")

    # 3. Copy final MP4 if the edit block has a render_path.
    render_path = (manifest.get("edit") or {}).get("render_path", "")
    if render_path:
        src_mp4 = Path(render_path)
        if not src_mp4.is_absolute():
            src_mp4 = Path.cwd() / src_mp4
        if src_mp4.exists():
            dest_mp4 = site_media / src_mp4.name
            shutil.copy2(src_mp4, dest_mp4)
            print(f"render    → {dest_mp4}")
        else:
            print(f"render_path {render_path!r} not found on disk; skipping MP4 copy")
    else:
        print("no edit.render_path in manifest; skipping MP4 copy")

    return 0


def _export_events(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_id, ts, kind, agent, shot_id, ref_event_id, payload "
            "FROM events ORDER BY event_id"
        ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            try:
                entry["payload"] = json.loads(entry["payload"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(entry)
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
