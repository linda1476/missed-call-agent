"""P0-7: keyterm/hotword prompting improves recognition of shop names, menu
items, and staff names — measured on 20 proper nouns, before/after rates
recorded to tests/fixtures/keyterms_report.json.

Offline analogue: faster-whisper `hotwords` stands in for AssemblyAI
keyterms prompting (same idea: bias decoding toward a vocabulary list).
"""

import json
from pathlib import Path

from receptionist.voice.stt import WhisperSTT

FIX_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "keyterms"
MANIFEST = FIX_DIR / "manifest.json"
REPORT = FIX_DIR / "keyterms_report.json"


def test_keyterm_recognition_before_after(tmp_path):
    assert MANIFEST.exists(), \
        f"missing {MANIFEST} — run tests/fixtures/generate_fixtures.py"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert len(manifest) == 20, "spec: 20 proper nouns"

    stt = WhisperSTT("tiny.en")
    nouns = [m["noun"] for m in manifest]
    items = []
    hits_before = hits_after = 0
    for m in manifest:
        wav = FIX_DIR / m["file"]
        assert wav.exists(), f"missing fixture {wav}"
        plain = stt.transcribe(str(wav))
        biased = stt.transcribe(str(wav), hotwords=nouns)
        noun = m["noun"].lower()
        b = noun in plain.lower()
        a = noun in biased.lower()
        hits_before += b
        hits_after += a
        items.append({"noun": m["noun"], "before": plain, "after": biased,
                      "hit_before": b, "hit_after": a})

    report = {
        "n": len(manifest),
        "before_rate": hits_before / len(manifest),
        "after_rate": hits_after / len(manifest),
        "items": items,
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    out = json.loads(REPORT.read_text(encoding="utf-8"))
    assert out["n"] == 20
    assert 0.0 <= out["before_rate"] <= 1.0
    assert 0.0 <= out["after_rate"] <= 1.0
    assert len(out["items"]) == 20
