"""Smoke test NVIDIA Magpie TTS.

Usage (from server/):
  .venv/bin/python scripts/smoke_tts.py [text ...]
  .venv/bin/python scripts/smoke_tts.py --voices
Writes scripts/out.wav. Never prints the key.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import httpx  # noqa: E402

import config  # noqa: E402
from voice import tts  # noqa: E402

OUT = Path(__file__).resolve().parent / "out.wav"
DEFAULT_TEXT = "Hi, I'm Twin. I put Redis next to the auth service."


def auth() -> dict:
    return {"Authorization": f"Bearer {config.NVIDIA_API_KEY}"}


async def list_voices(c: httpx.AsyncClient) -> int:
    t0 = time.perf_counter()
    r = await c.get(tts.MAGPIE_VOICES_URL, headers=auth())
    ms = (time.perf_counter() - t0) * 1000
    print(f"list_voices: HTTP {r.status_code} in {ms:.0f} ms")
    if r.status_code != 200:
        print(r.text[:500])
        return 1
    try:
        data = r.json()
    except ValueError:
        print(r.text[:2000])
        return 0
    print(json.dumps(data, indent=2)[:6000])
    return 0


async def synth_once(c: httpx.AsyncClient, text: str) -> int:
    form = {
        "text": text,
        "language": tts.LANGUAGE,
        "voice": tts.VOICE,
        "encoding": "LINEAR_PCM",
        "sample_rate_hz": str(tts.SAMPLE_RATE),
    }
    print(f"text: {text!r}")
    t0 = time.perf_counter()
    r = await c.post(tts.MAGPIE_SYNTH_URL, files={k: (None, v) for k, v in form.items()}, headers=auth())
    ms = (time.perf_counter() - t0) * 1000
    print(f"synthesize: HTTP {r.status_code} in {ms:.0f} ms, content-type={r.headers.get('content-type')}")
    if r.status_code != 200:
        print(r.text[:500])
        return 1
    body = r.content
    is_riff = body[:4] == b"RIFF"
    print(f"bytes: {len(body)}  starts with RIFF: {is_riff}")
    if not is_riff:
        print("NOTE: raw PCM returned; voice/tts.py wraps it in a WAV header (16-bit mono 44100).")
    wav = tts.ensure_wav(body)
    OUT.write_bytes(wav)
    secs = max(0, len(wav) - 44) / (tts.SAMPLE_RATE * 2)
    print(f"wrote {OUT} (~{secs:.2f}s of audio if 16-bit mono 44.1k)")

    # Also exercise the module path once (shared client)
    t0 = time.perf_counter()
    b = await tts.synthesize(text)
    ms2 = (time.perf_counter() - t0) * 1000
    print(f"tts.synthesize(): {'OK' if b else 'FAILED'} {len(b or b'')} bytes in {ms2:.0f} ms")
    await tts.aclose()
    return 0


async def main() -> int:
    args = sys.argv[1:]
    if not config.NVIDIA_API_KEY:
        print("ERROR: NVIDIA_API_KEY is not set. Add it to server/.env (see server/.env.example).")
        return 2
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            if "--voices" in args:
                return await list_voices(c)
            text = " ".join(a for a in args if not a.startswith("--")) or DEFAULT_TEXT
            return await synth_once(c, text)
    except httpx.HTTPError as e:
        print(f"ERROR: request failed: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
