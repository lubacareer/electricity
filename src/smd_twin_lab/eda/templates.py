"""Small, owned learning templates for the PCB Designer workspace."""

from __future__ import annotations

import hashlib
import json

from .model import (
    AssetKind,
    BoardDocument,
    BoardFootprint,
    BoardPad,
    BoardTrack,
    CopperLayer,
    EdaProjectDocument,
    LibraryAssetSnapshot,
    PointNm,
    SchematicDocument,
    SchematicLabel,
    SchematicPin,
    SchematicSymbol,
    SchematicWire,
    TeachingMetadata,
    mm,
    new_id,
)
from .repository import new_manifest


def blank_project(name: str = "Untitled PCB") -> EdaProjectDocument:
    """Create an empty, editable learning project."""

    return EdaProjectDocument(
        manifest=new_manifest(name),
        schematic=SchematicDocument(),
        board=BoardDocument(),
        teaching=TeachingMetadata(
            mode="learning",
            lesson_ids=("schematic_basics", "pcb_basics"),
            template_id="blank",
        ),
    )


def _pin(number: str, name: str, x_mm: float, y_mm: float) -> SchematicPin:
    return SchematicPin(new_id(), number, name, PointNm(mm(x_mm), mm(y_mm)))


def _asset(
    asset_id: str,
    kind: AssetKind,
    name: str,
    payload: dict[str, object],
) -> LibraryAssetSnapshot:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return LibraryAssetSnapshot(
        asset_id=asset_id,
        kind=kind,
        name=name,
        source="builtin://learning-templates",
        source_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        license_spdx="MIT",
        payload_json=encoded,
    )


def divider_project(name: str = "3.3 V Divider") -> EdaProjectDocument:
    """Create a two-resistor divider whose VOUT is analytically 1.65 V."""

    source_id = new_id()
    first_resistor_id = new_id()
    second_resistor_id = new_id()
    source_pins = (_pin("1", "+", 0, -5), _pin("2", "-", 0, 5))
    first_pins = (_pin("1", "1", -5, 0), _pin("2", "2", 5, 0))
    second_pins = (_pin("1", "1", -5, 0), _pin("2", "2", 5, 0))
    schematic = SchematicDocument(
        symbols=(
            SchematicSymbol(
                source_id,
                "V1",
                "3.3",
                "smdtwin:VDC",
                "voltage_source",
                PointNm(mm(10), mm(20)),
                source_pins,
                "smdtwin:Terminal_2",
            ),
            SchematicSymbol(
                first_resistor_id,
                "R1",
                "10k",
                "Device:R",
                "resistor",
                PointNm(mm(25), mm(15)),
                first_pins,
                "smdtwin:R_0603",
            ),
            SchematicSymbol(
                second_resistor_id,
                "R2",
                "10k",
                "Device:R",
                "resistor",
                PointNm(mm(40), mm(15)),
                second_pins,
                "smdtwin:R_0603",
            ),
        ),
        wires=(
            SchematicWire(
                new_id(),
                (PointNm(mm(10), mm(15)), PointNm(mm(20), mm(15))),
            ),
            SchematicWire(
                new_id(),
                (PointNm(mm(30), mm(15)), PointNm(mm(35), mm(15))),
            ),
            SchematicWire(
                new_id(),
                (
                    PointNm(mm(45), mm(15)),
                    PointNm(mm(45), mm(25)),
                    PointNm(mm(10), mm(25)),
                ),
            ),
        ),
        labels=(
            SchematicLabel(new_id(), "VCC", PointNm(mm(10), mm(15))),
            SchematicLabel(new_id(), "VOUT", PointNm(mm(30), mm(15))),
            SchematicLabel(new_id(), "GND", PointNm(mm(10), mm(25))),
        ),
    )

    def pad(number: str, x_mm: float, net: str) -> BoardPad:
        return BoardPad(
            new_id(),
            number,
            PointNm(mm(x_mm), 0),
            mm(1.4),
            mm(1.6),
            net,
        )

    board = BoardDocument(
        outline=(
            PointNm(0, 0),
            PointNm(mm(50), 0),
            PointNm(mm(50), mm(30)),
            PointNm(0, mm(30)),
            PointNm(0, 0),
        ),
        footprints=(
            BoardFootprint(
                new_id(),
                "V1",
                "smdtwin:Terminal_2",
                source_id,
                PointNm(mm(7), mm(15)),
                (pad("1", -2.5, "VCC"), pad("2", 2.5, "GND")),
                courtyard_width_nm=mm(7),
                courtyard_height_nm=mm(5),
            ),
            BoardFootprint(
                new_id(),
                "R1",
                "smdtwin:R_0603",
                first_resistor_id,
                PointNm(mm(23), mm(12)),
                (pad("1", -1, "VCC"), pad("2", 1, "VOUT")),
                courtyard_width_nm=mm(3.2),
                courtyard_height_nm=mm(2.4),
            ),
            BoardFootprint(
                new_id(),
                "R2",
                "smdtwin:R_0603",
                second_resistor_id,
                PointNm(mm(37), mm(18)),
                (pad("1", -1, "VOUT"), pad("2", 1, "GND")),
                courtyard_width_nm=mm(3.2),
                courtyard_height_nm=mm(2.4),
            ),
        ),
        tracks=(
            BoardTrack(
                new_id(),
                "VCC",
                PointNm(mm(4.5), mm(15)),
                PointNm(mm(4.5), mm(12)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "VCC",
                PointNm(mm(4.5), mm(12)),
                PointNm(mm(22), mm(12)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "VOUT",
                PointNm(mm(24), mm(12)),
                PointNm(mm(30), mm(12)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "VOUT",
                PointNm(mm(30), mm(12)),
                PointNm(mm(36), mm(18)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "GND",
                PointNm(mm(9.5), mm(15)),
                PointNm(mm(9.5), mm(22)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "GND",
                PointNm(mm(9.5), mm(22)),
                PointNm(mm(38), mm(22)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
            BoardTrack(
                new_id(),
                "GND",
                PointNm(mm(38), mm(22)),
                PointNm(mm(38), mm(18)),
                mm(0.25),
                CopperLayer.FRONT,
            ),
        ),
    )
    footprint_assets = (
        _asset(
            "smdtwin:Terminal_2",
            AssetKind.FOOTPRINT,
            "Terminal 2",
            {
                "courtyard_width_nm": mm(7),
                "courtyard_height_nm": mm(5),
                "pads": [
                    {
                        "pin_number": "1",
                        "x_nm": mm(-2.5),
                        "y_nm": 0,
                        "width_nm": mm(1.4),
                        "height_nm": mm(1.6),
                    },
                    {
                        "pin_number": "2",
                        "x_nm": mm(2.5),
                        "y_nm": 0,
                        "width_nm": mm(1.4),
                        "height_nm": mm(1.6),
                    },
                ],
            },
        ),
        _asset(
            "smdtwin:R_0603",
            AssetKind.FOOTPRINT,
            "R 0603",
            {
                "courtyard_width_nm": mm(3.2),
                "courtyard_height_nm": mm(2.4),
                "pads": [
                    {
                        "pin_number": "1",
                        "x_nm": mm(-1),
                        "y_nm": 0,
                        "width_nm": mm(1.0),
                        "height_nm": mm(1.1),
                    },
                    {
                        "pin_number": "2",
                        "x_nm": mm(1),
                        "y_nm": 0,
                        "width_nm": mm(1.0),
                        "height_nm": mm(1.1),
                    },
                ],
            },
        ),
    )
    return EdaProjectDocument(
        manifest=new_manifest(name),
        schematic=schematic,
        board=board,
        library_assets=footprint_assets,
        teaching=TeachingMetadata(
            mode="learning",
            lesson_ids=("voltage_divider", "schematic_to_pcb"),
            template_id="divider",
        ),
    )
