"""P0-9: the three demo scenarios replay green — first call, repeat call
with memory, concurrent slot race."""

from pathlib import Path

from receptionist.demo.scenarios import load_scenario, run_scenario

SCEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "scenarios"


def test_scenario_1_first_call(tmp_path):
    sc = load_scenario(SCEN_DIR / "1_first_call.json")
    res = run_scenario(sc, str(tmp_path / "s1.db"), store_id="demo1")
    assert res["ok"], res["failures"]


def test_scenario_2_repeat_call_memory(tmp_path):
    sc = load_scenario(SCEN_DIR / "2_repeat_call.json")
    res = run_scenario(sc, str(tmp_path / "s2.db"), store_id="demo2")
    assert res["ok"], res["failures"]


def test_scenario_3_concurrent_race(tmp_path):
    sc = load_scenario(SCEN_DIR / "3_concurrent.json")
    res = run_scenario(sc, str(tmp_path / "s3.db"), store_id="demo3")
    assert res["ok"], res["failures"]
