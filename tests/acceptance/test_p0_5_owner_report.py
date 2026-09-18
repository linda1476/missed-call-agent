"""P0-5: a finished call emits a schema-valid action-unit report and the
owner dashboard renders it."""

from receptionist.booking.slots import SlotTable
from receptionist.dashboard.app import render_dashboard
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.report.emit import validate_report
from receptionist.voice.pipeline import CallPipeline

STORE = "acceptance-p0-5"


def test_report_validates_and_dashboard_renders(tmp_path):
    db = tmp_path / "store.db"
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, ["fri-1900"])
    pipe = CallPipeline(store, slots, STORE,
                        extractor=DeterministicExtractor())

    wm = pipe.start_call("+14155550105")
    pipe.run_utterances(wm, [
        "Table for three on Friday at 7 PM, name is Rosa.",
        "Yes.",
    ])
    report = pipe.end_call(wm)

    assert validate_report(report) is True
    assert report["category"] == "booking_confirmed"
    assert report["action_items"], "report must carry action units"
    assert report["booking"]["slot_id"] == "fri-1900"

    html = render_dashboard(store, STORE)
    assert "fri-1900" in html or "Friday" in html or "fri" in html
    assert "booking_confirmed" in html
    assert "Rosa" in html or "party" in html.lower() or "3" in html
