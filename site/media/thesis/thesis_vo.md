# Thesis VO — script

Pairs with `site/media/demo.mp4` (37.17s). Voiceover delivered by an Irish
male voice via ElevenLabs (`voice_id qwaVDEGNsBllYcZO1ZOJ`, model
`eleven_multilingual_v2`, `speed=1.08`, then ffmpeg `atempo=1.0344` to
land at exactly 37.19s).

Spoken script:

> A human writes a brief.
> A logline, a duration, a few notes on tone.
> The pipeline never starts itself.
>
> The Producer is the only voice that issues work.
> Seven specialists wait — the Producer speaks; they answer.
>
> No agent talks to another. They write to the page.
> The Manifest is the only shared memory — coordination becomes archive.
>
> A render lands. The Judge reads it.
> A verdict is not advice — it is a hard constraint.
> The Creative Director can override,
> to keep the film, not just the shot, intact.
>
> The Editor reads the final rows. The film assembles.
>
> Magic Doors.
> Brief on one side. Film on the other.

To regenerate, see [scratch/generate_thesis_vo.py](../../../scratch/generate_thesis_vo.py).
The constant `SCRIPT` in that file is the source of truth; this document is a
human-readable mirror.

Output artifacts:
- `site/media/thesis/thesis_vo.mp3` — raw VO at speed=1.08 (38.45s)
- `site/media/thesis/thesis_vo_fitted.mp3` — atempo-adjusted to 37.19s
- `site/media/demo_vo.mp4` — demo video + VO muxed (37.17s, AAC 192k)
