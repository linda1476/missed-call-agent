"""P0-10: submission artifacts exist and README documents a 10-minute
quickstart (install / run / architecture / AssemblyAI feature list)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SUB = ROOT / "submission"


def test_submission_files_exist():
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert (SUB / "slides.pdf").exists(), "slides draft PDF missing"
    assert (SUB / "slides.pdf").read_bytes()[:5] == b"%PDF-"
    cover = SUB / "cover.png"
    assert cover.exists(), "cover image missing"
    assert cover.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (SUB / "video_storyboard.md").exists(), "video storyboard missing"
    assert (SUB / "description_short.txt").exists()
    assert (SUB / "description_long.txt").exists()


def test_storyboard_covers_required_cuts():
    text = (SUB / "video_storyboard.md").read_text(encoding="utf-8").lower()
    for cut in ("problem", "first call", "repeat call", "concurrent",
                "owner correction", "architecture"):
        assert cut in text, f"storyboard missing cut: {cut}"


def test_readme_quickstart():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for section in ("install", "run", "architecture", "assemblyai"):
        assert section in readme, f"README missing section: {section}"
    assert "pip install -r requirements.txt" in readme
    short = (SUB / "description_short.txt").read_text(encoding="utf-8")
    assert len(short.strip()) <= 400, "short description should be ~2 sentences"
