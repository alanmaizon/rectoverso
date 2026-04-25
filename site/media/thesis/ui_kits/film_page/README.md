# UI Kit — Film Page

The single-page film-press surface for Recto Verso Productions. Seven sections, all driven from the shot manifest + events JSON.

## Files

- `index.html` — the assembled page, paper mode, wired to the mock data.
- `page.css` — page-only styles (grid + layout). Uses `../../colors_and_type.css` for tokens.
- `app.jsx` — the React app; composes the components below.
- `Masthead.jsx` — fixed top rule + RV wordmark + provenance tag.
- `Hero.jsx` — poster / play / title / logline / metadata rail.
- `Brief.jsx` — festival-synopsis block.
- `Script.jsx` — numbered shot list (summary rows).
- `ShotStrip.jsx` — horizontal scrolling row of thumbnails; click expands `ShotDrawer`.
- `ShotDrawer.jsx` — full shot detail: prompt, routing, attempts, judge notes, cost.
- `AgentTrace.jsx` — vertical timeline from events.json, filterable by shot.
- `Ledger.jsx` — cost + latency + quota table.
- `Colophon.jsx` — how-it-was-made paragraph + repo link + credits.
- `data/` — symlinks-in-spirit to the manifest + events JSON.
- `media/shots/` — placeholder SVG frames (real MP4s drop in here).

## Fidelity notes

This is a recreation of the brief's *intent*, not of existing UI — there is no built UI in the repo. Every design decision here is traceable to the `site/README.md` brief. Where the brief was silent, defaults were picked to match the reference points (MUBI Notebook, Letterboxd single-film page, Criterion essay).
