from __future__ import annotations

from importlib.resources import files

from smd_twin_lab import paths
from smd_twin_lab.importers import load_bundle

LOCALIZED_PACKAGE_FILES = (
    "resources/i18n/smd_twin_lab_ru.qm",
    "resources/lessons/en/aoi.md",
    "resources/lessons/en/faults.md",
    "resources/lessons/en/getting_started.md",
    "resources/lessons/en/simulation.md",
    "resources/lessons/ru/aoi.md",
    "resources/lessons/ru/faults.md",
    "resources/lessons/ru/getting_started.md",
    "resources/lessons/ru/simulation.md",
)


def test_installed_package_sample_fallback(monkeypatch) -> None:
    monkeypatch.setattr(paths, "repository_root", lambda: None)

    sample = paths.sample_bundle_path()
    project = load_bundle(sample)

    assert project.name == "USB Sensor/Status Board"
    assert len(project.components) == 12
    assert project.spice_netlist_path is not None
    assert project.twin_manifest_path is not None


def test_installed_package_contains_localized_resources() -> None:
    package_root = files("smd_twin_lab")
    missing = [
        relative_path
        for relative_path in LOCALIZED_PACKAGE_FILES
        if not package_root.joinpath(*relative_path.split("/")).is_file()
    ]

    assert not missing, f"Missing installed localization resources: {missing}"
    assert package_root.joinpath("resources", "i18n", "smd_twin_lab_ru.qm").read_bytes()
