"""External executable discovery without mutating global PATH."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ToolPaths:
    kicad_cli: Path | None
    ngspice: Path | None
    renode: Path | None


def _first_existing(candidates: list[str | Path | None]) -> Path | None:
    for candidate in candidates:
        if not candidate:
            continue
        resolved = Path(candidate).expanduser()
        if resolved.is_file():
            return resolved.resolve()
    return None


def discover_tools() -> ToolPaths:
    workspace_root = Path(__file__).resolve().parents[2]
    kicad_from_path = shutil.which("kicad-cli")
    spice_from_path = shutil.which("ngspice_con") or shutil.which("ngspice")
    renode_from_path = shutil.which("renode") or shutil.which("Renode")

    kicad = _first_existing(
        [
            os.environ.get("SMD_TWIN_KICAD_CLI"),
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "KiCad"
            / "10.0"
            / "bin"
            / "kicad-cli.exe",
            kicad_from_path,
        ]
    )
    ngspice = _first_existing(
        [
            os.environ.get("SMD_TWIN_NGSPICE"),
            workspace_root / "Spice64" / "bin" / "ngspice_con.exe",
            workspace_root / ".tools" / "ngspice-47" / "bin" / "ngspice_con.exe",
            spice_from_path,
        ]
    )
    renode = _first_existing([os.environ.get("SMD_TWIN_RENODE"), renode_from_path])
    return ToolPaths(kicad_cli=kicad, ngspice=ngspice, renode=renode)
