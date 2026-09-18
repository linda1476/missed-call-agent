"""Generate audio fixtures with edge-tts (free neural TTS) + ffmpeg.

Run once locally:  python tests/fixtures/generate_fixtures.py
Outputs are committed so CI never needs TTS or network:

  call_01_booking.wav                 — P0-1 recorded-call fixture
  keyterms/noun_XX.wav + manifest.json — P0-7 proper-noun set (20)
"""

import asyncio
import json
import subprocess
from pathlib import Path

FIX = Path(__file__).resolve().parent
VOICE = "en-US-JennyNeural"

CALL_01 = ("Hi, I'd like to book a table for two on Friday at seven P M. "
           "My name is Dana.")

# 20 proper nouns a small shop's agent must catch: shop names, dishes,
# staff names — deliberately non-dictionary words.
NOUNS = [
    "Guadalupe", "Tzatziki", "Bouillabaisse", "Nguyen", "Xiomara",
    "Charcuterie", "Acai", "Worcester", "Szechuan", "Quesadilla",
    "Beauregard", "Gnocchi", "Tzimmes", "Katsudon", "Ezekiel",
    "Madeleine", "Prosciutto", "Joaquin", "Vichyssoise", "Oaxaca",
]
NOUN_SENT = "I have a reservation under {n}."


async def _tts(text: str, out_mp3: Path) -> None:
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE)
    await comm.save(str(out_mp3))


def _to_wav(mp3: Path, wav: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3), "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", str(wav)],
        check=True, capture_output=True)
    mp3.unlink()


def main() -> None:
    kt = FIX / "keyterms"
    kt.mkdir(exist_ok=True)

    mp3 = FIX / "call_01_booking.mp3"
    asyncio.run(_tts(CALL_01, mp3))
    _to_wav(mp3, FIX / "call_01_booking.wav")
    print("wrote call_01_booking.wav")

    manifest = []
    for i, noun in enumerate(NOUNS, 1):
        name = f"noun_{i:02d}.wav"
        mp3 = kt / f"noun_{i:02d}.mp3"
        asyncio.run(_tts(NOUN_SENT.format(n=noun), mp3))
        _to_wav(mp3, kt / name)
        manifest.append({"file": name, "noun": noun,
                         "sentence": NOUN_SENT.format(n=noun)})
        print(f"wrote {name} ({noun})")
    (kt / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                     encoding="utf-8")
    print("wrote manifest.json")


if __name__ == "__main__":
    main()
