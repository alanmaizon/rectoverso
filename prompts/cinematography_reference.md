# Cinematography reference — vocabulary + per-provider templates

Read this alongside `prompt_smith.md`. This file is the **vocabulary and templates**; prompt_smith.md is the **rules**. Together they teach PromptSmith to write professional-grade prompts tuned to each provider.

Not consumed by the runtime directly — PromptSmith pulls it in via its system prompt. Human-editable; update the templates when we learn that a provider responds better to different phrasing.

---

## Shot-type dictionary

Pick one shot type per prompt. Do not stack multiple. The shot type anchors the entire composition.

| Tag | Name | What you're showing | When to use |
|---|---|---|---|
| **EWS** | extreme wide shot | broad landscape, subject small or absent | scene establishers, solitude, environment-first |
| **WS** | wide shot | full subject + context around them | establishing a person in place |
| **MLS** | medium-long shot | subject from knees up with environment | action-within-context |
| **MS** | medium shot | subject waist-up | dialogue framing, gesture work |
| **MCU** | medium close-up | chest + head | emotional anchor, default conversational |
| **CU** | close-up | head + shoulders or object detail | key beats, reactions, texture of hands/objects |
| **ECU** | extreme close-up | feature fill — eye, mouth, a single object surface | rare emphasis beats |

## Lens focal length (35mm-equivalent)

Paired with shot type — redundant reinforcement for image models that understand lens language.

| Focal length | Angle of view | Feel | Typical pairing |
|---|---|---|---|
| 18–24mm | wide | immersive, distorted edges | EWS / WS for scale |
| 28–35mm | documentary | natural human perspective | WS / MLS for naturalism |
| 50mm | neutral | "normal" eye | MS for honesty |
| 85mm | short tele | flattering compression | MCU / CU for portraits |
| 100–135mm | tele | strong compression, isolated subject | CU for emotional intimacy |
| 200mm+ | long tele | heavy compression, bokeh'd backgrounds | ECU or distant subjects |

## Depth of field cues

Providers read aperture language well.

- **f/1.4, f/2** — "razor-thin depth of field, only the eyes in focus"
- **f/2.8** — "shallow depth of field, background softly diffused"
- **f/4, f/5.6** — "subject separated but environment legible"
- **f/8, f/11** — "deep focus, everything sharp" (doc or landscape)
- **f/16, f/22** — rarely useful in generated video; legibility collapses

## Camera movement vocabulary

Specific verbs image models recognize. Prefer these over invented phrases.

- **locked-off** — fixed tripod, no movement
- **handheld, subtle breathing** — minimal organic drift
- **handheld, following** — operator tracks the subject
- **dolly in / dolly out** — smooth forward/backward on tracks
- **slow push-in** — almost imperceptible dolly toward subject
- **pedestal up / down** — vertical move, camera height changes
- **pan left / right** — horizontal rotation from fixed position
- **tilt up / down** — vertical rotation from fixed position
- **whip pan** — fast pan, motion-blurred
- **orbit** — camera circles subject at consistent radius
- **arc** — partial orbit, narrative sweep
- **crane up / crane down** — large vertical move
- **steadicam follow** — smooth tracked motion alongside subject

Avoid: "cinematic camera movement" (no information), "dynamic camera" (ditto).

## Lighting setups

### Key + direction

- **overcast diffused key, frame-right** — soft, no hard shadows, directional
- **overhead practical (single bulb)** — motivated by a ceiling fixture
- **window key from frame-left** — daylight side-lit
- **low key, rim light only** — silhouette with edge separation
- **three-point (key + fill + back)** — studio-style, rarely wanted for naturalistic
- **ambient only, no motivated key** — flat, distributed

### Color temperature (Kelvin)

| Temp | Label | Look |
|---|---|---|
| 3200K | tungsten | warm amber |
| 4000K | warm white | mild amber |
| 5600K | daylight | neutral |
| 6500K | overcast | cool, slight cyan lean |
| 8000K+ | shade / cold | strong cyan-blue |

### Saturation + contrast direction

- **desaturated (-15 to -25)** — naturalistic drama lean
- **neutral** — documentary fidelity
- **punchy (+10 to +20)** — commercial, pop
- **crushed blacks** — noir / grade-down on shadows
- **lifted blacks** — dreamy / vintage flat
- **rolled-off highlights** — soft film-stock feel

## Palette grammar

Don't say "cold" alone — name the hues:

- **cyan-teal shadows + muted ochre highlights** (cool dusk / dawn)
- **deep umber shadows + warm peach skin tones** (golden hour)
- **desaturated cyan with slate-grey rocks** (overcast coastal)
- **emerald greens + mist cyan** (forest dawn)
- **bleached off-white walls + warm ochre practicals** (interior noir)

The more specific the palette, the less the provider invents one.

## Film emulation / grade

Providers trained on film stock imagery respond to named stocks.

- **Kodak 500T** — warm tungsten-balanced, medium grain, cinematic
- **Kodak Vision3 250D** — daylight, fine grain, commercial-clean
- **Fuji Eterna 8553** — muted, cyan-leaning shadows, naturalistic drama default
- **Arri Alexa flat grade** — digital neutrality, highest fidelity baseline
- **35mm anamorphic, lens flares** — widescreen blockbuster feel
- **digital, clean, no grain** — modern commercial / minimalist

Grain intensity: **fine**, **medium**, **heavy**. Pair with stock name.

## Atmosphere + texture

Specific atmospheric elements the prompt names get rendered; generic ones get ignored.

- **volumetric mist with slow lateral drift**
- **fine rain, visible streaks near practical lights**
- **moisture beading on stone / metal**
- **drifting dust motes, backlit**
- **snow particles, slow descent**
- **wind-driven leaves, medium density**
- **heat haze on asphalt**

## Mood tags

Single phrase, picked from this list, appended near the end:

quiet · contemplative · urgent · tense · dreamy · stark · hushed · watchful · melancholic · reverent · nostalgic · foreboding · serene · electric · desolate

---

## T2V vs I2V — the mode decision

**This distinction governs every template below. Get it wrong and the model drifts.**

- **T2V (text-to-video)**: the model paints the entire scene from nothing. The prompt must describe **what the scene looks like** — setting, palette, lighting, atmosphere, film stock. More specificity helps.
- **I2V (image-to-video)**: the reference image IS the scene. The model animates FROM that image forward in time. The prompt must describe **what happens over time** — motion phases, camera movement, character continuity. Scene details from T2V templates actively hurt: if the prompt says "windswept coast" and the reference is a forest path, the model drifts between the two and loses adherence on both.

PromptSmith decides I2V mode when:
1. `reference_image_paths` is non-empty (authoritative — a ref defines the scene regardless of endpoint), OR
2. The model id contains `image-to-video` / `reference-to-video` (fallback — model-name signal).

I2V templates are always **shorter** than T2V templates (30–80 words vs 60–140). The omitted tokens are already present in the reference.

## Per-provider prompt templates

Literal skeletons. PromptSmith fills the `{CURLY_TOKENS}` with values from the shot + brief, using vocabulary from the sections above. If a token doesn't apply (e.g. no atmosphere on a clean interior), drop the entire phrase — don't leave empty slots.

### Veo 3.1 Fast (`veo-3.1-fast-generate-001`) — natural cinematic paragraph

**Grammar**: flowing prose, one long paragraph. Explicit camera verbs + lens specs. Negatives embedded as "no X, no Y" inside the paragraph (Veo supports a negative field but the in-line form works more reliably). Length: 60–120 words.

```
{SHOT_TYPE}, {LENS_EQUIV} equivalent lens, {DOF_CUE}.
{SUBJECT_AND_ACTION — one or two sentences describing who/what is in frame and what they do}.
Camera: {CAMERA_MOVEMENT}.
Lighting: {LIGHTING_SETUP}, {COLOR_TEMP}, {SATURATION_DIRECTION}.
Palette: {PALETTE_SPECIFICS}.
Atmosphere: {ATMOSPHERE_TEXTURE}.
Film: {FILM_EMULATION}.
Mood: {MOOD_TAG}.
{EMBEDDED_NEGATIVES — "No X, no Y, no Z."}
```

Negative field: empty string. Never send `negative` to Veo in the initial pass; it underperforms vs embedded.

**Veo I2V (`image_base64` provided)** — rare path, but when a reference image is supplied, switch to the motion-only shape used by the Seedance I2V template above. Don't restate palette / atmosphere / film if the ref already carries them.

### Kling 2.1 Pro (`fal-ai/kling-video/v2.1/pro/image-to-video`) — I2V-only, tag-forward motion

**Always I2V.** Kling 2.1 on fal requires `image_url`; the reference IS the scene. The prompt describes motion, camera, and continuity — NOT scene setting. **Do not restate palette / lighting / location details** that are already in the reference image; the model drifts if you do. Uses native `negative_prompt` field. Length: 30–70 words total, tight.

```
{SUBJECT_AND_ACTION — one verb-forward sentence describing motion phases, e.g. "Subject turns head left, raises hand, exhales a slow breath"}.
Same person, same clothing, same location as reference image.
Tags: {CAMERA_MOVEMENT}, {SHOT_TYPE_TAG — only if motion changes framing; skip if matches ref}, {MOTION_SPEED_TAG — "slow", "deliberate", "unhurried"}, {MOOD_TAG}.
```

Negative field: comma-separated undesirables. Include `"scene change, background change, location change, different person, different clothing, morph"` to guard subject consistency. Then append appearance negatives from the shot: `"warm sunlight"` / `"saturated colors"` etc. Example: `"scene change, background change, different person, morph, warm sunlight, saturated colors, bad anatomy, text, watermark"`.

### Seedance 2.0 (`bytedance/seedance-2.0/...`) — dual mode

Seedance has T2V (`.../text-to-video`), I2V (`.../image-to-video`), and reference-to-video (`.../reference-to-video`) variants. Grammar DIFFERS by mode. No `negative_prompt` field in any variant — embed as "Avoid: X, Y".

#### Seedance T2V (`.../text-to-video`) — full scene description, natural language

Length: 80–140 words.

```
{SHOT_TYPE} — {SPATIAL_ANCHOR: "subject at frame-left third", "foreground in sharp focus with {BACKGROUND_ELEMENT} receding", etc.}.
{SUBJECT_AND_ACTION — two or three sentences; Seedance responds to verb-chain descriptions of motion phases}.
Camera movement: {CAMERA_MOVEMENT}.
Lighting: {LIGHTING_SETUP}, {COLOR_TEMP}, {PALETTE_SPECIFICS}.
Atmosphere: {ATMOSPHERE_TEXTURE}.
Film look: {FILM_EMULATION}.
Mood: {MOOD_TAG}.
Avoid: {NEGATIVES_AS_COMMA_LIST — e.g. "warm sunlight, multiple people, close-up portrait, saturated colors"}.
```

#### Seedance I2V (`.../image-to-video` or `.../reference-to-video`) — motion-only, reference defines the scene

**The reference image IS the scene — do NOT restate setting, palette, lighting, atmosphere, or film details. Describe only what happens over time.** Over-describing causes adherence drift (see the A/B failure at sh_005 v2). Length: 40–80 words.

```
Starting from the reference frame, the subject {ACTION_PHASE_1}, then {ACTION_PHASE_2}, then {ACTION_PHASE_3}.
Same subject, same clothing, same location as reference — do not change the scene.
Camera: {CAMERA_MOVEMENT}.
Motion pacing: {MOTION_SPEED — "slow", "deliberate", "unhurried"}.
Mood: {MOOD_TAG}.
Avoid: scene change, background change, different subject, morph, teleporting elements, {PROVIDER_APPROPRIATE_NEGATIVES — only appearance negatives that guard AGAINST the reference's own features if necessary, e.g. "warm sunlight" only if the reference could be misread as warm}.
```

Negative field for all Seedance variants: empty string. Seedance 2.0 will 422 on a separate negative_prompt; inline "Avoid:" is the only form.

### Alibaba Wan 2.7 Plus (`wan2.7-t2v`) — descriptive prose, palette-first

**Grammar**: descriptive prose heavy on palette and lighting. Wan handles physicality and materials well; camera jargon less so. Natural-language negatives go in the native `negative_prompt` field. Length: 40–80 words — Wan truncates past ~60.

```
{SUBJECT_AND_ACTION — one clear sentence}.
Setting: {LOCATION_AND_MATERIALS — "weathered stone, damp moss, cast-iron hardware"}.
Lighting: {LIGHTING_SETUP}, {COLOR_TEMP}, {PALETTE_SPECIFICS}.
Atmosphere: {ATMOSPHERE_TEXTURE}.
Camera: {SIMPLE_CAMERA_MOVEMENT — prefer "locked-off", "slow push-in", "gentle pan"; avoid dolly/crane jargon}.
Mood: {MOOD_TAG}.
```

Negative field: `{NEGATIVES_AS_COMMA_LIST — "warm sunlight, saturated colors, people in frame"}`

### Alibaba Wan 2.6 Turbo (`wan2.6-t2v`) — tighter prose, iteration tier

Same grammar as Wan 2.7 Plus. Keep to 40–60 words max — Turbo is faster but less forgiving on long prompts. Used for iteration loops; Plus for finals.

### DashScope Qwen-Image Plus (`qwen-image-plus`) — still-frame composition

**Grammar**: describe ONE decisive frame, not motion. Composition-first. No camera movement verbs. Length: 40–80 words.

```
A single cinematic still frame: {SUBJECT_AND_POSE — "{character} stands {pose}, {facing/orientation}"}.
{COMPOSITION_CUE — "subject at left third, {BACKGROUND_ELEMENT} receding into soft focus"}.
{LENS_AND_DOF — "{LENS_EQUIV} lens, shallow depth of field"}.
Lighting: {LIGHTING_SETUP}, {COLOR_TEMP}, {PALETTE_SPECIFICS}.
Atmosphere: {ATMOSPHERE_TEXTURE}.
Film look: {FILM_EMULATION}.
Mood: {MOOD_TAG}.
```

Negative field: `{NEGATIVES_AS_COMMA_LIST — "motion blur, multiple people, text, watermark, low resolution"}`

### Gemini Nano-banana (`gemini-2.5-flash-image`) — cinematic language, compact

**Grammar**: cinematic natural-language description, understands film brands and shot-type tags. No native negative_prompt — adapter folds negatives into the prompt as "Avoid: X, Y". Length: 40–100 words — past ~200 words adherence drops.

```
{SHOT_TYPE}, {LENS_EQUIV} equivalent.
{SUBJECT_AND_POSE — one tight sentence}.
{LIGHTING_SETUP}, {COLOR_TEMP}, {PALETTE_SPECIFICS}.
{ATMOSPHERE_TEXTURE}.
{FILM_EMULATION}.
{MOOD_TAG} mood.
Avoid: {NEGATIVES_AS_COMMA_LIST}.
Aspect ratio: {ASPECT}.
```

Negative field: empty (Gemini has no native field). Adapter already folds in the "Avoid:" and "Aspect ratio:" lines, but including them in the template output is harmless and keeps the prompts reproducible when PromptSmith's raw output is read as-is.

### Runway Gen-4 (placeholder)

Not currently integrated (Runway isn't on fal.ai; would require the Runway direct API). If we add it later, template will follow: short prompt (10–50 words), I2V-first, no negative_prompt. Until then, PromptSmith should never be asked to emit a Runway prompt; routing excludes it.

---

## Worked examples — why I2V mode is different

### sh_001 (T2V, Veo) — establishing lighthouse, no reference

```
WS, 24mm equivalent lens, deep focus at f/8.
A weathered stone lighthouse perched on a rocky headland at first light, grey mist lifting slowly from wet black rocks and tide pools below.
Camera: locked-off, very subtle handheld breathing.
Lighting: overcast diffused key, 6500K, -20 saturation, no direct sun.
Palette: cyan-teal shadows, muted ochre on lichen, bleached bone-grey sky.
Atmosphere: volumetric mist with slow lateral drift, moisture on stone.
Film: Fuji Eterna 8553, medium grain.
Mood: quiet, watchful.
No people, no boats, no warm sunlight, no lens flare.
```

Veo paints from scratch — the full scene description earns the adherence score.

### sh_005 (I2V, Seedance) — keeper steps through door, reference image present

Reference image: nano-banana frame showing the keeper in coat on a misty forest path with a lighthouse behind.

**Wrong (T2V-shaped — actual sh_005 v2, scored adherence 0.45):**

> Scene: exterior threshold of a weathered lighthouse on a windswept North Atlantic coast, late afternoon. The lighthouse keeper, a solitary figure in a heavy oilskin coat, grips the iron handle of a salt-worn wooden door and pushes it open on stiff hinges. He pauses on the threshold, shoulders squared against the wind... Overcast 5600K daylight as the key, raking in from camera-left... Palette: desaturated slate-grey and cyan-teal shadows... Fuji Eterna 8553, light 35mm grain...

Why wrong: the prompt describes "windswept North Atlantic coast" but the reference is a forest path. The model drifts between the two scene descriptions and under-delivers on both.

**Right (I2V-shaped — motion and continuity only):**

> Starting from the reference frame, the keeper walks forward toward the lighthouse door, grips the iron handle, then pushes the heavy door inward and begins stepping across the threshold.
> Same person, same coat, same location as reference — do not change the scene.
> Camera: handheld, subtle breathing, holding the figure's shoulder line.
> Motion pacing: slow, deliberate.
> Mood: quiet, watchful.
> Avoid: scene change, background change, teleporting forward, different person, morph.

Why right: the reference already defines "weathered lighthouse on a misty path" visually; re-describing it competes. The prompt focuses purely on what happens over time — three motion phases, camera movement, continuity anchor, mood.
