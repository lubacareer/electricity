from datetime import UTC, datetime
from pathlib import Path

from smd_twin_lab.history import RunHistory
from smd_twin_lab.models import FirmwareState, RunReport


def make_report(run_id: str, passed: bool = True) -> RunReport:
    timestamp = datetime.now(UTC).isoformat()
    return RunReport(
        schema_version=1,
        run_id=run_id,
        project_id="board-1",
        scenario_id="nominal",
        started_at=timestamp,
        completed_at=timestamp,
        passed=passed,
        infrastructure_error=False,
        firmware_state=FirmwareState.NORMAL,
        outputs={"green_led": True},
        measurements={"adc_voltage_v": 1.65},
        signals=(),
        timeline=(),
        explanations=(),
    )


def test_history_round_trip_and_upsert(tmp_path: Path) -> None:
    history = RunHistory(tmp_path / "runs.sqlite3")
    history.save(make_report("run-1"))
    history.save(make_report("run-1", passed=False))

    recent = history.recent("board-1")
    assert len(recent) == 1
    assert recent[0].passed is False
    assert history.load_json("run-1")["run_id"] == "run-1"


def test_history_unknown_run_returns_none(tmp_path: Path) -> None:
    history = RunHistory(tmp_path / "runs.sqlite3")
    assert history.load_json("missing") is None
