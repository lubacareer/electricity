import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from smd_twin_lab.history import RunHistory, localized_report_dict, write_localized_report
from smd_twin_lab.models import (
    Capability,
    CapabilityStatus,
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    FaultSpec,
    FirmwareState,
    MessageRef,
    ProjectCapabilities,
    RunReport,
    Scenario,
)
from smd_twin_lab.services import RuntimeServices
from smd_twin_lab.ui.sample_data import build_sample_project


class FakeRussianRenderer:
    def render(self, language: str, message_ref: MessageRef, fallback: str) -> str:
        assert language == "ru"
        if message_ref.message_id == "explanation.test":
            return f"Объяснение {message_ref.parameters['reference']}"
        if message_ref.message_id == "diagnostic.test":
            return "Диагностика"
        return fallback


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


def test_localized_report_payload_preserves_canonical_data_and_message_refs() -> None:
    report = make_report("localized-run")
    report = RunReport(
        **{
            **report.to_dict(),
            "explanations": ("English explanation R1",),
            "diagnostics": (
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    "TEST",
                    "English diagnostic",
                    message_ref=MessageRef("diagnostic.test"),
                ),
            ),
            "explanation_refs": (MessageRef("explanation.test", {"reference": "R1"}),),
        }
    )

    canonical = report.to_dict()
    localized = localized_report_dict(report, "ru", FakeRussianRenderer())

    assert "language" not in canonical
    assert canonical["explanations"] == ("English explanation R1",)
    assert localized["language"] == "ru"
    assert localized["explanations"] == ["Объяснение R1"]
    assert localized["diagnostics"][0]["message"] == "Диагностика"
    assert localized["diagnostics"][0]["code"] == "TEST"
    assert localized["explanation_refs"][0]["message_id"] == "explanation.test"


def test_history_and_manual_export_store_requested_presentation_language(tmp_path: Path) -> None:
    base = make_report("russian-run")
    report = RunReport(
        **{
            **base.to_dict(),
            "explanations": ("English explanation R2",),
            "explanation_refs": (MessageRef("explanation.test", {"reference": "R2"}),),
        }
    )
    history = RunHistory(tmp_path / "runs.sqlite3")
    history.save(report, language="ru", renderer=FakeRussianRenderer())

    stored = history.load_json(report.run_id)
    assert stored is not None
    assert stored["language"] == "ru"
    assert stored["explanations"] == ["Объяснение R2"]

    output = tmp_path / "reports" / "localized.json"
    write_localized_report(output, report, "ru", FakeRussianRenderer())
    exported = output.read_text(encoding="utf-8")
    assert '"language": "ru"' in exported
    assert "Объяснение R2" in exported

    with sqlite3.connect(history.database_path) as connection:
        version = connection.execute(
            "SELECT version FROM schema_info WHERE singleton = 1"
        ).fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
    assert version == 1
    assert "language" not in columns


def test_runtime_history_uses_language_captured_when_scenario_started() -> None:
    report = make_report("service-run")

    class StubSupervisor:
        def run(self, project, scenario):
            return report

    class RecordingHistory:
        language: str | None = None

        def save(self, saved_report, *, language: str = "en", renderer=None) -> None:
            assert saved_report is report
            self.language = language

    available = Capability(CapabilityStatus.AVAILABLE, "available")
    project = replace(
        build_sample_project(),
        project_id="sensor-status-v1",
        capabilities=ProjectCapabilities(available, available, available, available),
    )
    history = RecordingHistory()
    services = RuntimeServices(
        tools=None,
        importer=None,
        supervisor=StubSupervisor(),
        history=history,
        circuit_engine_name="test",
        firmware_engine_name="test",
    )
    scenario = Scenario(
        "run-language",
        "Nominal",
        25.0,
        FaultSpec(FaultKind.NONE),
        language="ru",
    )

    assert services.run_scenario(project, scenario) is report
    assert history.language == "ru"
