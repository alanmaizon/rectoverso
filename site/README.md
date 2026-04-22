# Recto Verso Productions — front-end brief

You are building the single-page, read-only web experience for a film produced by **Recto Verso Productions**, an autonomous multi-agent AI filmmaking pipeline called `rectoverso`.

Each film begins as a human-written brief and ends as an assembled short. The site is the gallery wall where one film is shown alongside the evidence of how it was made — the shot list, the agent decisions, the cost of production, and the editable composition ready for any agent or human to re-render. The name says it: **brief on one side, film on the other**.

This is **not** an app, **not** a dashboard, **not** a SaaS landing page. It is a film-press page in the register of MUBI, Letterboxd, *Sight & Sound*, or a Criterion essay site. Editorial. Generous whitespace. Monospace for metadata. Dark type on warm off-white paper, or light type on deep charcoal — pick one and commit.

## What the site renders

A single page with these sections, in order:

1. **Hero** — final film video player (looping MP4 or poster → play), film title, logline, duration, date. Secondary: a YouTube embed as failover, a **render-signature** line (`md5 <edit.render_md5>` + "bit-identical on every render") that sits as small-caps metadata under the player, a **Download composition** button shipping `media/composition.zip` (the Hyperframes `index.html` + `assets/` bundle — anyone can `npx hyperframes render` it locally and reproduce the MP4 byte-for-byte), and a one-line provenance tag ("Produced by rectoverso · Anthropic Claude Opus 4.7 · Hyperframes · Kling · Wan · Veo · ElevenLabs").

2. **Brief** — the creative input the pipeline received. Short. Treat it like a film-festival synopsis.

3. **Script** — the shot list the Screenwriter produced from the brief. Numbered. Each shot shows scene, description, duration, one-line note.

4. **Shot strip** — horizontal row of thumbnails (one per shot). Click a thumbnail and the card swaps its SVG for the shot's MP4, muted-autoplay-loop inline, so you can watch the clip without leaving the carousel. A small "playing" chip sits in the top-right of the active card. Clicking again (or any other card) stops it. Simultaneously the **drawer** opens below with: the authored prompt, the provider the Router chose, `attempts[]` history, final-render metadata, judge notes, cost, and the **audio parts** — each dialogue line and SFX cue associated with the shot as its own `<audio>` player (text for dialogue, description for SFX, shot-relative timing). This is the *evidence* layer: see the frame and hear every stem that sits under it.

5. **Agent trace** — vertical timeline of events from `data/events.json`. Producer, Screenwriter, Prompt Smith, Router, Renderer, Shot Judge, Audio Agent, Editor Agent, Creative Director. Show: agent name, event type, one-line detail, latency, cost. Group by shot where relevant.

6. **Production ledger** — a table: cost per provider (USD), tokens per agent (input/output/cache), total USD, total latency, Anthropic credits used, fal calls, Veo calls, Wan free-quota calls, ElevenLabs credits used. This is the "receipts" section.

7. **Colophon** — how it was made, in one paragraph. Link to the GitHub repo. Credits: human brief author, pipeline (rectoverso), models used. The Earth-Day timing hook if applicable.

## Data you read

Everything comes from two JSON files and an MP4. No backend. No build step beyond optional bundling.

- `data/manifest.json` — the shot manifest. Spec: [data.schema.md](data.schema.md).
- `data/events.json` — flattened export of the event log (agent calls, costs, tokens, latencies). Spec in [data.schema.md](data.schema.md).
- `media/final.mp4` — the assembled film.
- `media/shots/sh_XXX.mp4` — per-shot renders (matches `shots[].final.render_path`). Each doubles as the video that plays when the corresponding card in the Shot strip is clicked.
- `media/shots/sh_XXX.svg` — per-shot poster frame used when the card is not playing (and as the `poster` attribute on the inline `<video>`).
- `<audio>` sources for dialogue and SFX follow the paths in `manifest.audio.dialogue[].audio_path` and `manifest.audio.sfx[].audio_path` (typically `artifacts/audio/<id>.mp3` in the mock; pipeline runs populate the real paths).
- `media/composition.zip` — download artifact: the Hyperframes project (`index.html` + `assets/`) bundled as a zip. Anyone with Node + `npx hyperframes render` can re-render it locally, bit-identical to the hero MP4 on this page.

During design, use `mock/manifest.json`, `mock/events.json`, and a placeholder MP4. The real pipeline will produce drop-in replacements.

## Aesthetic

**Reference points** (mood, not imitation):
- MUBI Notebook — generous margins, restrained color.
- Letterboxd single-film page — metadata is *part of the design*, not buried.
- Criterion essay page — typography does the heavy lifting.
- A24 film site — confident use of one big hero video.

**Rules of the road**:
- One typeface for body (serif or clean grotesque), one monospace for metadata. No third typeface.
- No gradients. No drop shadows. No glassmorphism.
- No emojis. No dashboards. No "stats cards" with big green up-arrows.
- Use real editorial typographic scale. Large generous H1, clear small caps, proper widows/orphans control.
- Dark mode by default is fine; if you go light, make it warm (off-white, not pure white).
- The one place color earns its keep: the shot-strip thumbnails. Let the frames carry the color.
- Monospace numerals for costs, durations, timestamps.

**What it must not look like**:
- A Vercel docs page.
- A Notion dashboard.
- A SaaS pricing page.
- A crypto "AI agent" marketing site.

If in doubt, ask: *would this feel at home on a film studio's festival page?* If no, redo it.

## Technical constraints

- **Static only.** HTML + CSS + vanilla JS, or a minimal static-site framework (Astro welcome; Next.js acceptable only if you already know it cold). No database, no server, no API routes.
- **No build step preferred.** If you use a build, it must produce a plain static directory that can be dropped into any host.
- **Deployable by drag-and-drop** to Netlify, or by pushing to a `gh-pages` branch. No environment variables at runtime.
- **Single page** (sections scroll-anchored) is strongly preferred over a multi-page site. A second `/about` page is acceptable.
- **Video**: `<video>` tag with `preload="metadata"` for the shot strip (active card only — inactive cards render the SVG poster, not the video), `preload="auto"` only for the hero. Active strip cards use `muted autoplay loop playsInline` so playback starts on click without browser gesture prompts.
- **Audio**: one `<audio controls>` per part in the drawer, `preload="metadata"`. Native browser controls are fine — the editorial design stays out of the way. One player active at a time is user-discretion (we don't pause others).
- **Responsive**: desktop-first but mobile must not be broken. Phone sees a column layout; shot strip becomes vertical.
- **Accessibility**: all videos have captions or alt-text summaries; all interactive elements are keyboard-reachable.

## Scope boundaries — what you do NOT build

- No editor. No timeline scrubber. No in-browser compositing. If the user wants to edit, they download `composition.zip`, unpack, edit the HTML, and re-render locally. That's the product.
- No login. No user accounts. No comments.
- No real-time pipeline runner in the browser. Everything is after-the-fact.
- No analytics beyond a single privacy-respecting counter if desired (optional).
- No AI chat interface. The agents already ran; this site is the report.

## Output

A directory you can `git init && git push` and deploy. At minimum:

```
site/
  index.html
  styles.css
  script.js           (or equivalent)
  data/
    manifest.json     (symlink or copy from state/manifest.json in real run)
    events.json       (exported from state/events.db)
  media/
    final.mp4         (the film; also target of edit.render_path)
    composition.zip   (the Hyperframes project: index.html + assets/, re-renderable)
    shots/
      sh_001.mp4
      sh_001.svg      (poster for the inactive card)
      ...
  about.html          (optional)
```

The backend pipeline will populate `data/` and `media/` for the real submission. Your job is to make something beautiful that renders *any* valid manifest.

## One sentence to hold in your head

**Recto Verso Productions is a film studio whose camera is a pipeline. The page you are building is where the resulting film lives, with all its receipts.**
