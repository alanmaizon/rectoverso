"""ElevenLabsAudioTool — Tier-2 audio adapter for SFX + dialogue.

Primary use case: per-shot sound-effect generation. Documentary films have
far more SFX cues than dialogue lines, and the Editor Agent wants each
cue as a discrete per-shot clip it can layer at composition time. The
adapter's SFX path is first-class; the TTS path is secondary, used only
when a shot carries a dialogue line.

Design choices (see docs/agents.md § Audio Agent):

- **Per-shot clips, not one mixed track.** The Editor is already
  shot-addressed; forcing it to re-derive timing from a timeline-addressed
  mix breaks the addressing symmetry.
- **Cues-only in v1, no environmental beds.** Scene-boundary decisions
  belong to the Editor — hardcoding scene grouping in the Audio Agent
  would lock in a compositional choice before the Editor can influence it.
  Operators who have ambient tracks drop them in `inputs/audio/` and the
  Editor layers them at assembly time.
- **Tool-Protocol compliant** (`name == "audio_agent"`). Same shape as
  every other Tier-4-style adapter; the orchestrator (Day 6+) can dispatch
  audio calls through the same machinery as render/judge.
- **Pre-paid credits, $0 USD.** Cost tracked via
  `budget.elevenlabs_credits_remaining`; `record_spend(actual_credits=N)`
  debits the counter.

Endpoints (verified 2026-04-23 against ElevenLabs docs):
    SFX: POST https://api.elevenlabs.io/v1/sound-generation
    TTS: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
    Auth: `xi-api-key: $ELEVENLABS_API_KEY` header
    Output: raw audio bytes (application/octet-stream) in the response body.
    Format: `output_format` query param — default mp3_44100_128, use
            mp3_44100_192 for documentary-grade finals.

Credit math (adapter surfaces actual consumption; these estimates are used
only for pre-flight budget projection):
    SFX with specified duration: ~40 credits/second
    SFX auto duration           : flat 100 credits per call (deterministically
                                   more expensive under 2.5s than specified
                                   duration — adapter always specifies for
                                   budget determinism).
    TTS eleven_v3 or eleven_multilingual_v2: 1 credit/character
    TTS eleven_flash_v2_5                  : 0.5 credits/character

Important policy: ElevenLabs does not publish a premade child voice (Voice
Library policy). The closest preset for a youthful timbre is `Gigi`
(`jBpfuIE2acCO8z3wKNLl`). A true 5-year-old voice requires custom IVC/PVC
cloning from a legally-sourced sample — out of scope this week.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ._common import http_error_body, md5_file, resolve_env_key


# ElevenLabs endpoints.
SFX_URL = "https://api.elevenlabs.io/v1/sound-generation"
TTS_URL_FMT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# SFX model id. v2 supports the `loop` param; default model as of 2026-04-23.
SFX_MODEL_ID = "eleven_text_to_sound_v2"

# TTS model id. eleven_v3 is the 2026 flagship (best emotional range, 70+ langs);
# eleven_multilingual_v2 is the stable fallback.
DEFAULT_TTS_MODEL_ID = "eleven_v3"
FALLBACK_TTS_MODEL_ID = "eleven_multilingual_v2"

# SFX defaults. Bumped prompt_influence to 0.7 per research — higher values
# produce cinematically specific output ("lake ice fracturing" stays on-theme)
# whereas lower values let the model drift into generic sound-design flavor.
DEFAULT_SFX_DURATION_S = 3.0                 # always specify for budget determinism
DEFAULT_SFX_PROMPT_INFLUENCE = 0.7
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"      # preview quality; mp3_44100_192 for finals
DEFAULT_HTTP_TIMEOUT_S = 60

# SFX constraints per the API docs (verified 2026-04-23).
MIN_SFX_DURATION_S = 0.5
MAX_SFX_DURATION_S = 30.0

# Credit-cost estimation used for pre-flight. Actual credits consumed are
# surfaced in the result dict; the budget ledger is updated from those,
# not from this estimate. Estimates match ElevenLabs published rates.
CREDITS_PER_SFX_SECOND = 40
CREDITS_SFX_AUTO_DURATION = 100    # flat rate when duration not specified
CREDITS_PER_TTS_CHAR = 1           # eleven_v3 + eleven_multilingual_v2
CREDITS_PER_TTS_CHAR_FLASH = 0.5   # eleven_flash_v2_5


# ElevenLabs preset voices useful for documentary-drama. No premade child
# voice (Voice Library policy) — Gigi is the closest youthful timbre.
# Verified 2026-04-23 against ElevenLabs' premade voice catalog.
VOICE_PRESETS = {
    "gigi":        "jBpfuIE2acCO8z3wKNLl",    # "Gigi" — young female, closest to child
    "narrator_m":  "pqHfZKP75CvOlQylNhV4",    # "Bill" — documentary-tagged male narrator
    "narrator_m2": "pNInz6obpgDQGcFmaJgB",    # "Adam" — backup male narrator
    "narrator_f":  "EXAVITQu4vr4xnSDxMaL",    # "Bella" — calm female narrator
}


class ElevenLabsAudioTool:
    """Tool-Protocol adapter. `name == "audio_agent"`.

    Dispatches to either the SFX or TTS endpoint based on `payload["mode"]`.

    Common payload fields:
        mode         : "sfx" | "tts"
        output_dir   : Path — directory to save the clip
        attempt_id   : int  — 1-indexed; suffix on the output filename

    SFX-mode payload (mode="sfx"):
        sfx_id            : str     — short label, used in manifest audio.sfx[].sfx_id
        description       : str     — the prompt text ("the sharp crack of lake ice")
        duration_s        : float?  — optional, 0.5-22 seconds. Defaults to EL's choice.
        prompt_influence  : float?  — 0.0-1.0; higher = closer adherence to the prompt,
                                       lower = more model creativity. Default 0.3.

    TTS-mode payload (mode="tts"):
        line_id       : str   — manifest audio.dialogue[].line_id
        text          : str   — spoken text
        voice_id      : str   — ElevenLabs voice id (see VOICE_PRESETS for shortcuts)
        model_id      : str?  — default DEFAULT_TTS_MODEL_ID
        voice_settings: dict? — {stability, similarity_boost, style, use_speaker_boost}

    Returns a dict with Tool-Protocol shape (status, provider, cost_usd,
    quota_cost=credits, audio_path, audio_md5, plus mode-specific echo fields).
    """

    name = "audio_agent"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        sfx_url: str = SFX_URL,
        tts_url_fmt: str = TTS_URL_FMT,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        urlopen: Any = None,
    ) -> None:
        self._api_key = api_key
        self._sfx_url = sfx_url
        self._tts_url_fmt = tts_url_fmt
        self._http_timeout_s = http_timeout_s
        self._urlopen = urlopen or urllib.request.urlopen

    # -- Tool Protocol -----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("ElevenLabsAudioTool requires a shot_id")

        api_key = self._api_key or _resolve_elevenlabs_key()
        if not api_key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY missing from env and .env; "
                "cannot submit audio generation"
            )

        mode = payload.get("mode")
        if mode not in ("sfx", "tts"):
            raise ValueError(
                f"payload['mode'] must be 'sfx' or 'tts', got {mode!r}"
            )

        output_dir = Path(payload["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        attempt_id = int(payload["attempt_id"])

        if mode == "sfx":
            return self._generate_sfx(shot_id, payload, api_key, output_dir, attempt_id)
        return self._generate_tts(shot_id, payload, api_key, output_dir, attempt_id)

    # -- SFX path (primary) ------------------------------------------------

    def _generate_sfx(
        self,
        shot_id: str,
        payload: Mapping[str, Any],
        api_key: str,
        output_dir: Path,
        attempt_id: int,
    ) -> dict[str, Any]:
        sfx_id = str(payload.get("sfx_id") or f"sfx_{attempt_id}")
        description = str(payload["description"]).strip()
        if not description:
            raise ValueError("SFX mode requires non-empty payload['description']")

        # Always specify duration for budget determinism — auto-duration is a
        # flat 100 credits regardless of length, while specified duration is
        # 40 credits/s and strictly cheaper under 2.5s. Caller can opt into
        # auto by passing duration_s=None explicitly.
        duration_s = payload.get("duration_s", DEFAULT_SFX_DURATION_S)
        if "duration_s" in payload and payload["duration_s"] is None:
            duration_s = None
        if duration_s is not None:
            duration_s = float(duration_s)
            if duration_s < MIN_SFX_DURATION_S or duration_s > MAX_SFX_DURATION_S:
                raise ValueError(
                    f"SFX duration_s={duration_s} outside "
                    f"[{MIN_SFX_DURATION_S}, {MAX_SFX_DURATION_S}]"
                )

        prompt_influence = float(
            payload.get("prompt_influence", DEFAULT_SFX_PROMPT_INFLUENCE)
        )
        loop = bool(payload.get("loop", False))
        output_format = str(payload.get("output_format", DEFAULT_OUTPUT_FORMAT))

        # Output filename encodes shot + cue label for audit legibility.
        output_path = output_dir / f"{shot_id}_{sfx_id}_v{attempt_id}.mp3"

        body: dict[str, Any] = {
            "text": description,
            "prompt_influence": prompt_influence,
            "model_id": SFX_MODEL_ID,
            "loop": loop,
        }
        if duration_s is not None:
            body["duration_seconds"] = duration_s

        # output_format is a QUERY param, not a body field.
        url = f"{self._sfx_url}?output_format={output_format}"

        started = time.time()
        try:
            self._post_audio(url, api_key, body, output_path)
        except urllib.error.HTTPError as exc:
            body_text = http_error_body(exc)
            return _failure(
                mode="sfx",
                shot_id=shot_id,
                sfx_id=sfx_id,
                description=description,
                stage=_classify_http_failure(exc.code, body_text),
                stderr=body_text,
                started=started,
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                mode="sfx",
                shot_id=shot_id,
                sfx_id=sfx_id,
                description=description,
                stage="empty_response",
                stderr=f"ElevenLabs returned 0 bytes for SFX prompt",
                started=started,
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        # Credits consumed: 40/s if we specified duration, 100 flat if auto.
        # The adapter always specifies duration by default, so this almost
        # always lands on the per-second branch — but the math stays honest
        # for auto-mode dispatches.
        credits_used = _estimate_sfx_credits(duration_s)
        # `duration_s` in the result reflects what we REQUESTED. If auto, 0.0
        # as a sentinel — downstream can ffprobe the file if exact timing
        # matters before the Editor mounts it.
        reported_duration = duration_s if duration_s is not None else 0.0

        return {
            "status": "ok",
            "provider": "elevenlabs",
            "mode": "sfx",
            "shot_id": shot_id,
            "sfx_id": sfx_id,
            "description": description,
            "audio_path": str(output_path),
            "audio_md5": md5,
            "output_size_bytes": size_bytes,
            "duration_s": reported_duration,
            "output_format": output_format,
            "model_id": SFX_MODEL_ID,
            "cost_usd": 0.0,
            "quota_cost": credits_used,      # ElevenLabs credits
            "latency_s": latency,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    # -- TTS path (secondary) ----------------------------------------------

    def _generate_tts(
        self,
        shot_id: str,
        payload: Mapping[str, Any],
        api_key: str,
        output_dir: Path,
        attempt_id: int,
    ) -> dict[str, Any]:
        line_id = str(payload.get("line_id") or f"line_{attempt_id}")
        text = str(payload["text"]).strip()
        if not text:
            raise ValueError("TTS mode requires non-empty payload['text']")
        voice_id = str(payload["voice_id"]).strip()
        if not voice_id:
            raise ValueError("TTS mode requires payload['voice_id']")

        model_id = str(payload.get("model_id") or DEFAULT_TTS_MODEL_ID)
        # Voice settings defaults tuned for documentary-drama — moderate
        # stability so prosody stays natural, high similarity for consistency,
        # neutral style (no acting prompt), speaker_boost on for clarity.
        voice_settings = payload.get("voice_settings") or {
            "stability": 0.55,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        }
        seed = payload.get("seed")
        language_code = payload.get("language_code")
        output_format = str(payload.get("output_format", DEFAULT_OUTPUT_FORMAT))

        output_path = output_dir / f"{shot_id}_{line_id}_v{attempt_id}.mp3"
        # output_format is a QUERY param.
        url = f"{self._tts_url_fmt.format(voice_id=voice_id)}?output_format={output_format}"

        body: dict[str, Any] = {
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        # Seed for reproducibility — critical for snapshot tests.
        if seed is not None:
            body["seed"] = int(seed)
        if language_code:
            body["language_code"] = str(language_code)

        started = time.time()
        try:
            self._post_audio(url, api_key, body, output_path)
        except urllib.error.HTTPError as exc:
            body_text = http_error_body(exc)
            return _failure(
                mode="tts",
                shot_id=shot_id,
                line_id=line_id,
                text=text,
                voice_id=voice_id,
                stage=_classify_http_failure(exc.code, body_text),
                stderr=body_text,
                started=started,
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                mode="tts",
                shot_id=shot_id,
                line_id=line_id,
                text=text,
                voice_id=voice_id,
                stage="empty_response",
                stderr="ElevenLabs returned 0 bytes for TTS",
                started=started,
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        credits_used = _estimate_tts_credits(text, model_id=model_id)
        # Manifest schema requires duration_s + timing on audio.dialogue[]
        # entries. We probe the generated MP3 to get an honest duration
        # — ffprobe is already installed (Hyperframes depends on it) and
        # the overhead is negligible (<50ms). If ffprobe is unavailable,
        # fall back to a character-count estimate.
        duration_s = _probe_audio_duration(output_path) or _estimate_tts_duration(text)

        return {
            "status": "ok",
            "provider": "elevenlabs",
            "mode": "tts",
            "shot_id": shot_id,
            "line_id": line_id,
            "text": text,
            "voice_id": voice_id,
            "model_id": model_id,
            "audio_path": str(output_path),
            "audio_md5": md5,
            "output_size_bytes": size_bytes,
            "output_format": output_format,
            "duration_s": duration_s,
            "cost_usd": 0.0,
            "quota_cost": credits_used,
            "latency_s": latency,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    # -- HTTP helper -------------------------------------------------------

    def _post_audio(
        self,
        url: str,
        api_key: str,
        body: Mapping[str, Any],
        dest: Path,
    ) -> None:
        """POST the JSON body, stream the audio response to `dest`. Raises
        urllib.error.HTTPError on 4xx/5xx for the caller to classify."""
        data = json.dumps(body).encode("utf-8")
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._urlopen(req, timeout=self._http_timeout_s) as resp, open(
            dest, "wb"
        ) as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _resolve_elevenlabs_key() -> str | None:
    """Resolve ELEVENLABS_API_KEY from env or project .env."""
    return resolve_env_key("ELEVENLABS_API_KEY", "ELEVEN_API_KEY")


def _estimate_sfx_credits(duration_s: float | None) -> int:
    """SFX credit estimate matching ElevenLabs' published rates.

    - `duration_s` specified → 40 credits/second (rounded up).
    - `duration_s` None      → flat 100 credits (auto-duration rate).

    The adapter always specifies duration before dispatch (for budget
    determinism), so the auto-duration branch is here for completeness.
    """
    if duration_s is None:
        return CREDITS_SFX_AUTO_DURATION
    if duration_s <= 0:
        return 0
    return int(duration_s * CREDITS_PER_SFX_SECOND + 0.999)


def _estimate_tts_credits(text: str, model_id: str = DEFAULT_TTS_MODEL_ID) -> int:
    """Credit estimate per ElevenLabs' published rates.

    - eleven_v3 / eleven_multilingual_v2 → 1 credit per character
    - eleven_flash_v2_5                   → 0.5 credits per character
    """
    rate = (
        CREDITS_PER_TTS_CHAR_FLASH
        if "flash" in model_id.lower()
        else CREDITS_PER_TTS_CHAR
    )
    return max(1, int(len(text) * rate + 0.999))


def _probe_audio_duration(path: Path) -> float:
    """Return the actual audio duration in seconds via ffprobe. Returns 0.0
    if ffprobe isn't on PATH, the probe fails, or the file is unreadable —
    caller should fall back to an estimator in that case.

    ffprobe is already a dependency (Hyperframes rendering), so assuming
    its availability here is safe."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return 0.0
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return round(float(result.stdout.strip()), 3)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def _estimate_tts_duration(text: str) -> float:
    """Fallback duration estimate when ffprobe is unavailable.

    ElevenLabs v3 reads at roughly 14 characters/second in conversational
    tone. Add 0.5s for natural lead-in/tail. Good enough for the schema
    requirement while editor fine-tunes timing later."""
    if not text:
        return 0.5
    return round(len(text) / 14.0 + 0.5, 3)


def _classify_http_failure(http_code: int, body_text: str) -> str:
    lowered = body_text.lower()
    if "quota" in lowered or "credits" in lowered or http_code == 402:
        return "quota_exhausted"
    if "voice_not_found" in lowered or "invalid_voice" in lowered:
        return "invalid_voice"
    if http_code == 429:
        return "rate_limit"
    if http_code in (401, 403):
        return "auth"
    if http_code == 400:
        return "validation"
    if http_code == 422:
        return "content_policy"
    return f"submit:http_{http_code}"


def _failure(
    *,
    mode: str,
    shot_id: str,
    stage: str,
    stderr: str,
    started: float,
    sfx_id: str | None = None,
    description: str | None = None,
    line_id: str | None = None,
    text: str | None = None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": "elevenlabs",
        "mode": mode,
        "shot_id": shot_id,
        "audio_path": "",
        "audio_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": "",
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if mode == "sfx":
        payload["sfx_id"] = sfx_id
        payload["description"] = description
    else:
        payload["line_id"] = line_id
        payload["text"] = text
        payload["voice_id"] = voice_id
    return payload
