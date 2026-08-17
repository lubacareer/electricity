from dataclasses import replace
from pathlib import Path

import pytest

from smd_twin_lab.cli import main
from smd_twin_lab.models import FaultKind, FaultSpec, Scenario
from smd_twin_lab.services import build_runtime_services


def test_demo_nominal_and_fault(capsys, tmp_path: Path) -> None:
    nominal_path = tmp_path / "nominal.json"
    assert main(["demo", "--engine", "reference", "--output", str(nominal_path)]) == 0
    assert "NORMAL" in capsys.readouterr().out
    assert nominal_path.is_file()

    assert main(["demo", "--engine", "reference", "--fault", "thermistor_open"]) == 0
    assert "SENSOR_FAULT" in capsys.readouterr().out


def test_tools_reports_sample(capsys) -> None:
    assert main(["tools"]) == 0
    output = capsys.readouterr().out
    assert "KiCad CLI:" in output
    assert "USB Sensor/Status Board" in output


def test_runtime_refuses_to_fabricate_results_for_unknown_projects() -> None:
    services = build_runtime_services(prefer_external_spice=False)
    project = replace(services.load_sample(), project_id="unqualified-project")

    with pytest.raises(ValueError, match="no supported circuit/firmware plugin"):
        services.run_scenario(
            project,
            Scenario("unsupported", "Unsupported", 25.0, FaultSpec(FaultKind.NONE)),
        )
