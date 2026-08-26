from __future__ import annotations

from pathlib import Path

import pytest

from smd_twin_lab.eda.library import LibraryCatalog, LibraryKind
from smd_twin_lab.eda.model import AssetKind


def _write_fixture_catalog(root: Path) -> tuple[Path, Path, Path]:
    symbols = root / "symbols"
    footprints = root / "footprints"
    symbols.mkdir()
    footprints.mkdir()
    symbol_file = symbols / "Learning.kicad_sym"
    symbol_file.write_text(
        """(kicad_symbol_lib
  (version 20231120)
  (symbol "R"
    (property "Reference" "R")
    (symbol "R_0_1" (rectangle (start 0 0) (end 1 1)))
  )
  (symbol "VDC"
    (property "Sim.Device" "V")
  )
)
""",
        encoding="utf-8",
    )
    pretty = footprints / "Learning.pretty"
    pretty.mkdir()
    footprint_file = pretty / "R_0603.kicad_mod"
    footprint_file.write_text(
        '(footprint "R_0603" (version 20240108) (generator "fixture"))\n',
        encoding="utf-8",
    )

    nested = root / "nested-table"
    nested.write_text(
        """(sym_lib_table
  (version 7)
  (lib (name "Device") (type "KiCad")
       (uri "${FIXTURE_SYMBOL_DIR}/Learning.kicad_sym")
       (options "") (descr "Learning symbols"))
)
""",
        encoding="utf-8",
    )
    top = root / "sym-lib-table"
    top.write_text(
        f"""(sym_lib_table
  (version 7)
  (lib (name "Nested") (type "Table") (uri "{nested.as_posix()}")
       (options "") (descr "Nested table"))
)
""",
        encoding="utf-8",
    )
    footprint_table = root / "fp-lib-table"
    footprint_table.write_text(
        """(fp_lib_table
  (version 7)
  (lib (name "Resistor_SMD") (type "KiCad")
       (uri "${FIXTURE_FOOTPRINT_DIR}/Learning.pretty")
       (options "") (descr "Learning footprints"))
)
""",
        encoding="utf-8",
    )
    return top, footprint_table, footprint_file


def test_catalog_recurses_tables_searches_and_resolves_assets(tmp_path: Path) -> None:
    symbol_table, footprint_table, footprint_file = _write_fixture_catalog(tmp_path)
    catalog = LibraryCatalog(
        tmp_path / "catalog.sqlite3",
        table_paths=(symbol_table, footprint_table),
        kicad_cli=tmp_path / "missing-kicad-cli.exe",
        environment={
            "FIXTURE_SYMBOL_DIR": str(tmp_path / "symbols"),
            "FIXTURE_FOOTPRINT_DIR": str(tmp_path / "footprints"),
        },
    )

    report = catalog.refresh()

    assert report.symbol_count == 2
    assert report.footprint_count == 1
    assert not report.diagnostics
    assert [item.identifier for item in catalog.search("resistor")] == ["Resistor_SMD:R_0603"]
    assert catalog.get("Device:R_0_1") is None  # nested graphical unit is not a part

    voltage_source = catalog.resolve("Device:VDC")
    assert voltage_source.capabilities.ngspice_model
    assert not voltage_source.capabilities.render
    assert len(voltage_source.source_hash) == 64

    resistor = catalog.resolve("Device:R")
    assert "Sim.Device" not in resistor.raw_source
    assert not resistor.capabilities.ngspice_model

    snapshot = catalog.snapshot("Device:R")
    assert snapshot.asset_id == "Device:R"
    assert snapshot.kind is AssetKind.SYMBOL
    assert '"raw_s_expression":"(symbol \\"R\\"' in snapshot.payload_json

    footprint = catalog.resolve("Resistor_SMD:R_0603")
    assert footprint.summary.kind is LibraryKind.FOOTPRINT
    assert footprint.summary.source_path == str(footprint_file.resolve())
    assert not footprint.capabilities.eda_export


def test_catalog_reports_missing_and_unsupported_libraries(tmp_path: Path) -> None:
    table = tmp_path / "sym-lib-table"
    table.write_text(
        """(sym_lib_table
  (version 7)
  (lib (name "Remote") (type "Database") (uri "remote-db") (options ""))
  (lib (name "Missing") (type "KiCad") (uri "missing.kicad_sym") (options ""))
)
""",
        encoding="utf-8",
    )
    catalog = LibraryCatalog(
        tmp_path / "catalog.sqlite3",
        table_paths=(table,),
        kicad_cli=tmp_path / "missing.exe",
        environment={},
    )

    report = catalog.refresh()

    assert {diagnostic.code for diagnostic in report.diagnostics} == {
        "LIBRARY_PATH_MISSING",
        "LIBRARY_TYPE_UNSUPPORTED",
    }


def test_preview_requires_available_kicad_cli(tmp_path: Path) -> None:
    symbol_table, footprint_table, _ = _write_fixture_catalog(tmp_path)
    catalog = LibraryCatalog(
        tmp_path / "catalog.sqlite3",
        table_paths=(symbol_table, footprint_table),
        kicad_cli=tmp_path / "missing.exe",
        environment={
            "FIXTURE_SYMBOL_DIR": str(tmp_path / "symbols"),
            "FIXTURE_FOOTPRINT_DIR": str(tmp_path / "footprints"),
        },
    )
    catalog.refresh()

    with pytest.raises(FileNotFoundError, match="kicad-cli"):
        catalog.render_preview("Device:R", tmp_path / "preview")
