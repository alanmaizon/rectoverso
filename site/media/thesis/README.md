# Recto Verso Productions -- Design System

> *Brief on one side, film on the other.*

Recto Verso Productions is a film studio whose camera is a multi-agent AI pipeline (`rectoverso`). A human writes a brief; a Producer orchestrator coordinates specialist agents (Screenwriter, Prompt Smith, Router, Renderer, Shot Judge, Audio, Editor, Creative Director) to assemble a 30-60s short film with full provenance -- shot list, agent decisions, costs, and an FCPXML ready for a human editor.

The **product surface** is not the pipeline. It is the **film-press page** where a finished film is shown alongside all its receipts.

## Sources

- **Repo:** `alanmaizon/rectoverso` (private, GitHub) -- imported `site/README.md`, `site/data.schema.md`, `site/mock/manifest.json`, `site/mock/events.json`, `prompts/producer.md`, `prompts/creative_director.md`.
- **No pre-existing UI code, logo, fonts, or imagery.** The `site/` directory in the repo is a **written brief**, not a built site. Everything visual in this design system is authored here from that brief, in the register the brief specifies (MUBI Notebook / Letterboxd / Criterion / A24).
- No Figma was attached.

## What this design system covers

- **Visual foundations:** color (paper + charcoal), editorial type scale, monospace for metadata, spacing, rules, caption style, no-shadow/no-gradient constraints.
- **Content fundamentals:** tone, casing, voice, what not to say.
- **Iconography:** minimal. Lucide via CDN at 1.5 stroke weight as the chosen icon set.
- **UI kit -- Film Page** (`ui_kits/film_page/`) -- the seven-section single-page film-press surface described in the brief: Hero, Brief, Script, Shot Strip, Agent Trace, Production Ledger, Colophon.

## Index

| File | Purpose |
|---|---|
| `README.md` | This document. |
| `colors_and_type.css` | CSS variables -- color + type tokens + semantic styles. |
| `SKILL.md` | Agent-skill entry point for reusing this system. |
| `fonts/` | Webfonts (see #Fonts). |
| `assets/` | Logo lockups, rule ornaments, placeholder frames. |
| `preview/` | Design-system cards (registered assets). |
| `ui_kits/film_page/` | The primary UI kit -- interactive film-press page. |
| `site/` | Source material imported from the repo. |
| `prompts/` | Agent system prompts (voice reference). |

---

## Content fundamentals

**Voice.** Editorial and restrained. Reads like a festival programme note or a Criterion essay. Present tense for what the page shows ("The door opens onto a glacier"); past tense for pipeline events ("Router chose Kling Pro"). Never marketing-speak. Never second-person sales ("You'll love..."). Third-person and neutral first-person plural are fine ("The pipeline selected...", "We chose Kling Pro for humans").

**Casing.** Sentence case for everything except proper nouns and the masthead. **No Title Case Headings.** No ALL-CAPS shouting. Small-caps are used sparingly, only for section eyebrows ("shot 05 / scene 2").

**Numbers & metadata.** Monospace. Zeros are slashed (`ss01` feature on) when the face supports it. ISO timestamps (`2026-04-22 11:46:17`). Durations as `00:07` or `7.0s`. Money as `$3.20` with two decimals; free quota items as `$0.00` (never "free"). Large counts grouped with thin space or comma per locale.

**Em dashes, not hyphens.** `--` between thoughts; `-` for ranges (`$0-$15`); `-` only in compound words.

**Never.** No emoji. No "AI-powered", "next-gen", "revolutionary", "seamless". No exclamation marks. No rhetorical questions in the chrome. No "learn more" CTAs -- this is a reading surface, not a funnel.

**Examples (from the brief).**

- Logline: *"Magic doors appear in cities worldwide. Each leads somewhere surprising."*
- Provenance tag: *"Produced by rectoverso - Anthropic Claude Opus 4.7 - Kling - Wan - Veo - ElevenLabs"*
- Artistic note: *"The door should feel ordinary, not magical. It is just there."*
- Judge note: *"Figure floats above ice surface (no contact shadow). Ice texture repeats. Scale unconvincing."*
- Creative pivot: *"Accept attempt 2 and let Editor crop wider in post to recover scale."*

These are the register. Everything written in the product should sound like it belongs next to them.

---

## Visual foundations

**Mode.** Ships with **both** a paper light mode (warm off-white `#F4EFE6` on deep ink `#1C1A17`) and a deep-charcoal dark mode (`#14120F` paper, `#E8E2D6` ink). The film page picks one and commits; it does not provide a user toggle. Default is paper.

**Accent colour.** A single restrained vermilion `#C3432B` -- used only for the masthead rule, the active shot indicator, and download/link hover. Never for buttons-as-CTAs (there are none).

**Grayscale.** Everything else is a warm neutral. Cool grays are banned; all neutrals carry a trace of ochre so the page reads like paper, not a dashboard.

**Type.**
- **Serif (body + display):** GT Sectra-family feel. Substituted with **Source Serif 4** (Google Fonts) at 400/600 italic. Flag: the brief calls for a refined editorial serif; Sectra, Tiempos, or Canela would be the production choice.
- **Mono (metadata, numerals, code):** **JetBrains Mono** 400/500 with `ss01` slashed-zero on.
- **No sans-serif.** If ever needed, use the serif at small optical weight. One face for body, one for mono. No third typeface.

**Scale** (paper, 1.25 modular on a 16px base; see `colors_and_type.css`):

| Token | px | Usage |
|---|---|---|
| `--t-masthead` | 88 | Film title only. Italic. |
| `--t-h1` | 56 | Section opener. |
| `--t-h2` | 36 | Sub-section. |
| `--t-h3` | 24 | Shot card title. |
| `--t-body` | 18 | Running prose. Line-height 1.55. |
| `--t-small` | 14 | Caption, small caps. |
| `--t-mono` | 13 | Metadata, numerals. |

**Spacing.** Generous. Base unit `--sp-1` = 8px. The page breathes on multiples of 4 and 8, with column gutters at 32-64px and section margins at 96-160px. Mobile collapses to 24px gutters / 64px section margins.

**Backgrounds.** Flat paper or flat charcoal. No gradients. No patterns. No textures. No hand-drawn illustrations. The only non-flat surface on the page is **the video frame** -- the shot thumbnail or hero player. Colour lives *inside the frames*, never on the chrome.

**Borders & rules.** 1px hairline rules in `--fg-rule` are the primary divider. No boxes, no cards with borders-on-all-sides. A rule above and a rule below is a section; the sides are free.

**Cards.** There are essentially no "cards" in the UI-kit sense. Shot entries are rows separated by hairline rules, with a 16:9 thumbnail anchoring each row. If a surface absolutely must feel contained (the expanded shot drawer), use a 1px rule on all four sides at `--fg-rule`, zero radius, no shadow.

**Corner radius.** `--radius-0: 0` (default, everywhere). `--radius-1: 2px` (only on inline code chips and the play button hit target). That is it. No pill shapes. No large rounded cards.

**Shadows.** None. Ever. The brief is explicit: *"No drop shadows. No glassmorphism."* Elevation is achieved by whitespace and rule weight, not blur.

**Transparency & blur.** None. The only opacity used is text dimming (`--fg-2` is `--fg-1` at 68%). No frosted glass, no backdrop-filter.

**Animation.** Minimal and slow. 160-240ms, `cubic-bezier(0.2, 0, 0, 1)` for drawer opens. Video thumbnails fade in as they enter the viewport (200ms opacity 0 -> 1). No bounces, no springs, no shimmer loaders -- a static skeleton with a hairline placeholder rule is the loading state. Scroll is default browser-native; no smooth-scroll hijack.

**Hover.** Links shift ink to vermilion `--accent` and gain a 1px underline at current colour. Thumbnails lift by dimming ambient rows to 60% (not by scaling). No transform on hover. No shadow on hover.

**Press.** Links drop to `--accent-press` (`#9E311E`). Thumbnail press dims 80 -> 100 on the target and does not scale.

**Focus.** 2px solid outline in `--accent`, 2px offset. Always visible on keyboard focus.

**Layout.** Asymmetric 12-column with generous outer margins. Body copy capped at 62ch. Metadata slots into a right-hand mono rail on desktop, collapses below the content on mobile. Tables (ledger) are full-bleed with hairline rules between rows only -- no vertical rules, no zebra stripes.

**Imagery tone.** Warm. Golden-hour or blue-hour. Grain welcomed. Never cool corporate blue, never desaturated tech-ad grey. The shot-strip thumbnails carry all colour in the page; everything else is paper + ink.

---

## Iconography

The brief is an anti-icon brief: no emoji, no stats-card glyphs, no up-arrow trends. Icons are used *only* for functional affordances -- play, pause, download, external link, expand/collapse -- never decorative.

**Chosen set:** **Lucide** (`https://unpkg.com/lucide-static@0.469.0/icons/<name>.svg`), loaded from CDN. Stroke weight **1.5**, size **16-20px**, `currentColor`. Lucide chosen over Heroicons/Feather because its letterforms and rule weights sit well with a serif page.

**Substitution flag.** The brief does not specify an icon system. Lucide is a design-system substitution -- the user should confirm or swap for a bespoke mark set in production.

**Logo / masthead.** There is no logo file in the repo. The design system defines a **wordmark** -- the letters `RV` set in Source Serif Italic at the masthead size, with a hairline rule above and the full name `Recto Verso Productions` set in the mono face beneath at `--t-small` in small caps. This is the canonical mark for this system; `assets/wordmark.svg` holds it. If a production logo arrives, it replaces the SVG; nothing else changes.

**Emoji.** Never.

**Unicode chars as icons.** Only the em dash `--` and middle dot `-` as typographic separators. No `->`, no `yes`, no stars. If an arrow is needed, use Lucide `arrow-up-right`.

---

## Fonts

| Face | Role | File | Status |
|---|---|---|---|
| **Gragio** | Titles only (masthead, H1, H2, H3, display) | `fonts/gragio.otf`, `fonts/gragio.ttf` | Brand file - installed |
| **Pregio** | Body, lead, small, prose, blockquote | `fonts/Pregio-{Light,Regular,Medium,SemiBold,Bold}.ttf` | Brand file - installed (300/400/500/600/700) |
| JetBrains Mono | Metadata + numerals | Google Fonts `@import` | Working substitute |

**Two brand faces installed.** Gragio carries the *titles only* -- masthead italic, H1, H2, H3, and any display specimen. Everything else editorial (lead, body, small, blockquote, prose) is **Pregio** across five weights (Light/Regular/Medium/SemiBold/Bold). Mono remains JetBrains Mono; swap if the brand has a preferred mono.


---

## UI kits

- [`ui_kits/film_page/`](ui_kits/film_page/) -- the single-page film-press surface. All seven sections, wired to `site/mock/manifest.json` and `site/mock/events.json`. Interactive: shot strip expands into a drawer on click; agent trace filters by shot.

---

## Caveats

- No production logo, no typeface license, no image assets provided. Everything visual is authored from the written brief.
- `assets/` contains placeholder film frames authored as flat SVG compositions with colour blocks representing shots -- clearly labelled as placeholders. Real per-shot MP4s will drop in at `ui_kits/film_page/media/shots/` when the pipeline runs.
- Font substitution flagged above.
