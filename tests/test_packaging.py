from __future__ import annotations

from smd_twin_lab import paths
from smd_twin_lab.importers import load_bundle


def test_installed_package_sample_fallback(monkeypatch) -> None:
    monkeypatch.setattr(paths, "repository_root", lambda: None)

    sample = paths.sample_bundle_path()
    project = load_bundle(sample)

    assert project.name == "USB Sensor/Status Board"
    assert len(project.components) == 12
    assert project.spice_netlist_path is not None
    assert project.twin_manifest_path is not None
