"""Atomic persistence for editable ``.smdeda`` project packages."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

from .model import (
    EDA_SCHEMA_VERSION,
    AssetKind,
    BoardDocument,
    BoardFootprint,
    BoardPad,
    BoardSide,
    BoardTrack,
    BoardVia,
    CopperLayer,
    DesignRulesProfile,
    EdaProjectDocument,
    KiCadBridgeState,
    LibraryAssetSnapshot,
    NetClass,
    PinElectricalType,
    PointNm,
    ProjectManifest,
    SchematicDocument,
    SchematicJunction,
    SchematicLabel,
    SchematicPin,
    SchematicSymbol,
    SchematicWire,
    TeachingMetadata,
    new_id,
)

PACKAGE_FORMAT = "smdeda"
PACKAGE_VERSION = 1
_PACKAGE_ENTRY = "document.json"
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class EdaPackageError(ValueError):
    """Raised when an editable package is missing, corrupt, or unsupported."""


def _point(payload: dict[str, Any]) -> PointNm:
    return PointNm(int(payload["x_nm"]), int(payload["y_nm"]))


def document_to_dict(document: EdaProjectDocument) -> dict[str, Any]:
    """Return the canonical, deterministic JSON representation of a document."""

    # Every enum in the domain model is a StrEnum, so ``asdict`` produces a
    # JSON-safe tree while retaining the explicit schema owned by the model.
    return asdict(document)


def document_from_dict(payload: dict[str, Any]) -> EdaProjectDocument:
    """Decode and validate a schema-1 document payload."""

    if not isinstance(payload, dict):
        raise TypeError("document root must be an object")
    manifest_payload = payload["manifest"]
    schematic_payload = payload["schematic"]
    board_payload = payload["board"]

    manifest = ProjectManifest(
        schema_version=int(manifest_payload["schema_version"]),
        project_id=str(manifest_payload["project_id"]),
        name=str(manifest_payload["name"]),
        revision=int(manifest_payload.get("revision", 0)),
        created_at=str(manifest_payload.get("created_at", "")),
        generator=str(manifest_payload.get("generator", "smd_twin_lab")),
    )
    symbols = tuple(
        SchematicSymbol(
            symbol_id=str(item["symbol_id"]),
            reference=str(item["reference"]),
            value=str(item["value"]),
            library_id=str(item["library_id"]),
            kind=str(item["kind"]),
            position=_point(item["position"]),
            pins=tuple(
                SchematicPin(
                    pin_id=str(pin["pin_id"]),
                    number=str(pin["number"]),
                    name=str(pin["name"]),
                    offset=_point(pin["offset"]),
                    electrical_type=PinElectricalType(
                        pin.get("electrical_type", PinElectricalType.PASSIVE)
                    ),
                    required=bool(pin.get("required", True)),
                )
                for pin in item.get("pins", ())
            ),
            footprint_id=str(item.get("footprint_id", "")),
            rotation_deg=int(item.get("rotation_deg", 0)),
        )
        for item in schematic_payload.get("symbols", ())
    )
    schematic = SchematicDocument(
        symbols=symbols,
        wires=tuple(
            SchematicWire(
                wire_id=str(item["wire_id"]),
                points=tuple(_point(point) for point in item["points"]),
            )
            for item in schematic_payload.get("wires", ())
        ),
        junctions=tuple(
            SchematicJunction(
                junction_id=str(item["junction_id"]),
                position=_point(item["position"]),
            )
            for item in schematic_payload.get("junctions", ())
        ),
        labels=tuple(
            SchematicLabel(
                label_id=str(item["label_id"]),
                text=str(item["text"]),
                position=_point(item["position"]),
            )
            for item in schematic_payload.get("labels", ())
        ),
    )

    footprints = tuple(
        BoardFootprint(
            footprint_id=str(item["footprint_id"]),
            reference=str(item["reference"]),
            library_id=str(item["library_id"]),
            symbol_id=str(item["symbol_id"]),
            position=_point(item["position"]),
            pads=tuple(
                BoardPad(
                    pad_id=str(pad["pad_id"]),
                    pin_number=str(pad["pin_number"]),
                    offset=_point(pad["offset"]),
                    width_nm=int(pad["width_nm"]),
                    height_nm=int(pad["height_nm"]),
                    net=str(pad.get("net", "")),
                    shape=str(pad.get("shape", "rect")),
                    layers=tuple(
                        CopperLayer(layer) for layer in pad.get("layers", (CopperLayer.FRONT,))
                    ),
                    drill_nm=int(pad.get("drill_nm", 0)),
                )
                for pad in item.get("pads", ())
            ),
            rotation_deg=int(item.get("rotation_deg", 0)),
            side=BoardSide(item.get("side", BoardSide.FRONT)),
            courtyard_width_nm=int(item.get("courtyard_width_nm", 0)),
            courtyard_height_nm=int(item.get("courtyard_height_nm", 0)),
        )
        for item in board_payload.get("footprints", ())
    )
    board = BoardDocument(
        outline=tuple(_point(point) for point in board_payload.get("outline", ())),
        footprints=footprints,
        tracks=tuple(
            BoardTrack(
                track_id=str(item["track_id"]),
                net=str(item["net"]),
                start=_point(item["start"]),
                end=_point(item["end"]),
                width_nm=int(item["width_nm"]),
                layer=CopperLayer(item.get("layer", CopperLayer.FRONT)),
            )
            for item in board_payload.get("tracks", ())
        ),
        vias=tuple(
            BoardVia(
                via_id=str(item["via_id"]),
                net=str(item["net"]),
                position=_point(item["position"]),
                diameter_nm=int(item["diameter_nm"]),
                drill_nm=int(item["drill_nm"]),
            )
            for item in board_payload.get("vias", ())
        ),
        net_classes=tuple(
            NetClass(
                name=str(item.get("name", "Default")),
                nets=tuple(str(net) for net in item.get("nets", ())),
                clearance_nm=int(item.get("clearance_nm", 200_000)),
                track_width_nm=int(item.get("track_width_nm", 250_000)),
                via_diameter_nm=int(item.get("via_diameter_nm", 600_000)),
                via_drill_nm=int(item.get("via_drill_nm", 300_000)),
            )
            for item in board_payload.get("net_classes", ({"name": "Default"},))
        ),
        copper_layers=tuple(
            CopperLayer(layer)
            for layer in board_payload.get("copper_layers", (CopperLayer.FRONT, CopperLayer.BACK))
        ),
    )
    rules_payload = payload.get("rules", {})
    rules = DesignRulesProfile(
        minimum_track_width_nm=int(rules_payload.get("minimum_track_width_nm", 200_000)),
        minimum_clearance_nm=int(rules_payload.get("minimum_clearance_nm", 200_000)),
        minimum_via_diameter_nm=int(rules_payload.get("minimum_via_diameter_nm", 500_000)),
        minimum_via_drill_nm=int(rules_payload.get("minimum_via_drill_nm", 250_000)),
        copper_to_edge_nm=int(rules_payload.get("copper_to_edge_nm", 250_000)),
    )
    assets = tuple(
        LibraryAssetSnapshot(
            asset_id=str(item["asset_id"]),
            kind=AssetKind(item["kind"]),
            name=str(item["name"]),
            source=str(item["source"]),
            source_hash=str(item["source_hash"]),
            license_spdx=str(item.get("license_spdx", "NOASSERTION")),
            payload_json=str(item.get("payload_json", "{}")),
        )
        for item in payload.get("library_assets", ())
    )
    teaching_payload = payload.get("teaching", {})
    bridge_payload = payload.get("kicad_bridge", {})
    document = EdaProjectDocument(
        manifest=manifest,
        schematic=schematic,
        board=board,
        rules=rules,
        library_assets=assets,
        teaching=TeachingMetadata(
            mode=str(teaching_payload.get("mode", "learning")),
            lesson_ids=tuple(str(item) for item in teaching_payload.get("lesson_ids", ())),
            template_id=str(teaching_payload.get("template_id", "blank")),
        ),
        kicad_bridge=KiCadBridgeState(
            managed_project_path=bridge_payload.get("managed_project_path"),
            baseline_hash=bridge_payload.get("baseline_hash"),
            uuid_mappings=tuple(
                (str(first), str(second))
                for first, second in bridge_payload.get("uuid_mappings", ())
            ),
        ),
        compliance_case_ids=tuple(str(item) for item in payload.get("compliance_case_ids", ())),
    )
    _validate_identifiers(document)
    return document


def _validate_identifiers(document: EdaProjectDocument) -> None:
    identifiers: list[str] = []
    identifiers.extend(symbol.symbol_id for symbol in document.schematic.symbols)
    identifiers.extend(pin.pin_id for symbol in document.schematic.symbols for pin in symbol.pins)
    identifiers.extend(wire.wire_id for wire in document.schematic.wires)
    identifiers.extend(junction.junction_id for junction in document.schematic.junctions)
    identifiers.extend(label.label_id for label in document.schematic.labels)
    identifiers.extend(footprint.footprint_id for footprint in document.board.footprints)
    identifiers.extend(
        pad.pad_id for footprint in document.board.footprints for pad in footprint.pads
    )
    identifiers.extend(track.track_id for track in document.board.tracks)
    identifiers.extend(via.via_id for via in document.board.vias)
    if any(not identifier for identifier in identifiers):
        raise ValueError("design object identifiers must not be empty")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("design object identifiers must be unique")


class EdaProjectRepository:
    """Create, load, atomically save, and autosave editable projects."""

    def __init__(self, autosave_root: Path | None = None) -> None:
        self._autosave_root = autosave_root or (
            Path(user_data_path("SmdTwinLab", "SmdTwinLab")) / "designer-autosaves"
        )

    def create(
        self,
        name: str = "Untitled PCB",
        *,
        template_id: str = "blank",
    ) -> EdaProjectDocument:
        if template_id == "divider":
            from .templates import divider_project

            return divider_project(name)
        if template_id != "blank":
            raise ValueError(f"unknown project template: {template_id}")
        from .templates import blank_project

        return blank_project(name)

    def load(self, path: Path) -> EdaProjectDocument:
        source = Path(path)
        try:
            with zipfile.ZipFile(source, "r") as package:
                if _PACKAGE_ENTRY not in package.namelist():
                    raise EdaPackageError("package does not contain document.json")
                info = package.getinfo(_PACKAGE_ENTRY)
                if info.file_size > _MAX_DOCUMENT_BYTES:
                    raise EdaPackageError("editable project document is too large")
                raw = package.read(info)
            envelope = json.loads(raw.decode("utf-8"))
            if envelope.get("format") != PACKAGE_FORMAT:
                raise EdaPackageError("file is not an SMD EDA package")
            if envelope.get("package_version") != PACKAGE_VERSION:
                raise EdaPackageError(
                    f"unsupported package version: {envelope.get('package_version')!r}"
                )
            return document_from_dict(envelope["document"])
        except EdaPackageError:
            raise
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            zipfile.BadZipFile,
        ) as error:
            raise EdaPackageError(f"cannot load editable project {source}: {error}") from error

    def save(self, document: EdaProjectDocument, path: Path) -> Path:
        destination = Path(path)
        _validate_identifiers(document)
        destination.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "format": PACKAGE_FORMAT,
            "package_version": PACKAGE_VERSION,
            "document": document_to_dict(document),
        }
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            info = zipfile.ZipInfo(_PACKAGE_ENTRY, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            with zipfile.ZipFile(temporary, "w") as package:
                package.writestr(info, encoded)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def autosave(self, document: EdaProjectDocument) -> Path:
        self._autosave_root.mkdir(parents=True, exist_ok=True)
        return self.save(document, self.autosave_path(document.project_id))

    @property
    def autosave_root(self) -> Path:
        return self._autosave_root

    def list_autosaves(self) -> tuple[Path, ...]:
        if not self._autosave_root.is_dir():
            return ()
        return tuple(
            sorted(
                self._autosave_root.glob("*.smdeda"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                reverse=True,
            )
        )

    def autosave_path(self, project_id: str) -> Path:
        safe_project_id = "".join(
            character for character in project_id if character.isalnum() or character in "-_"
        )
        if not safe_project_id:
            raise ValueError("project_id has no safe filename characters")
        return self._autosave_root / f"{safe_project_id}.smdeda"


def new_manifest(name: str) -> ProjectManifest:
    """Create a schema-1 manifest with an RFC 3339 UTC timestamp."""

    return ProjectManifest(
        schema_version=EDA_SCHEMA_VERSION,
        project_id=new_id(),
        name=name,
        created_at=datetime.now(UTC).isoformat(),
    )
