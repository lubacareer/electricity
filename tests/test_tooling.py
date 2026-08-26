from pathlib import Path

import smd_twin_lab.tooling as tooling


def test_environment_tool_override_has_priority(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "ngspice_con.exe"
    executable.touch()
    monkeypatch.setenv("SMD_TWIN_NGSPICE", str(executable))
    monkeypatch.setattr(tooling.shutil, "which", lambda _name: None)

    assert tooling.discover_tools().ngspice == executable.resolve()


def test_first_existing_ignores_missing_candidates(tmp_path: Path) -> None:
    executable = tmp_path / "tool.exe"
    executable.touch()

    assert tooling._first_existing([None, tmp_path / "missing.exe", executable]) == (
        executable.resolve()
    )
