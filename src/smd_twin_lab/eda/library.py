"""Lazy, read-only indexing of installed KiCad symbol and footprint libraries.

The catalog deliberately treats library discovery, rendering, export support,
and simulation support as separate capabilities.  A part may be searchable
without being safe to place, export, or simulate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..engines.process import run_isolated_process
from ..tooling import discover_tools
from .model import AssetKind, LibraryAssetSnapshot


class LibraryKind(StrEnum):
    SYMBOL = "symbol"
    FOOTPRINT = "footprint"


@dataclass(frozen=True, slots=True)
class LibraryDiagnostic:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryPartSummary:
    """Cheap searchable metadata; resolving the source is a separate step."""

    identifier: str
    kind: LibraryKind
    library: str
    name: str
    source_path: str
    description: str = ""
    table_type: str = "KiCad"
    native_supported: bool = True


@dataclass(frozen=True, slots=True)
class LibraryCapabilities:
    render: bool
    eda_export: bool
    internal_dc_model: bool
    ngspice_model: bool


@dataclass(frozen=True, slots=True)
class ResolvedLibraryAsset:
    summary: LibraryPartSummary
    source_hash: str
    raw_source: str
    capabilities: LibraryCapabilities
    license_id: str | None
    license_notice: str | None


@dataclass(frozen=True, slots=True)
class CatalogRefreshReport:
    symbol_count: int
    footprint_count: int
    library_count: int
    diagnostics: tuple[LibraryDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class _TableEntry:
    name: str
    table_type: str
    uri: str
    description: str
    source_table: Path


_LIB_FORM_RE = re.compile(r"\(lib\s+(?P<body>.*?)\)\s*(?=\(lib|\)\s*$)", re.DOTALL)
_FIELD_RE = re.compile(r'\((?P<key>[a-z_]+)\s+"(?P<value>(?:\\.|[^"\\])*)"\)')
_ENV_RE = re.compile(r"\$\{([^}]+)\}")
_SIMULATION_MARKERS = ("Sim.Device", "Spice_Model", "Spice_Primitive", "Spice_Lib_File")
_INTERNAL_DC_SYMBOLS = {
    "Device:R",
    "Device:R_Small",
    "Simulation_SPICE:VDC",
    "Simulation_SPICE:VDC_1",
}


def _unescape_table_value(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _balanced_lib_forms(text: str) -> Iterable[str]:
    """Yield complete ``(lib ...)`` forms without needing a full parser."""

    index = 0
    while True:
        start = text.find("(lib", index)
        if start < 0:
            return
        depth = 0
        quoted = False
        escaped = False
        for cursor in range(start, len(text)):
            char = text[cursor]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield text[start : cursor + 1]
                    index = cursor + 1
                    break
        else:
            return


def _parse_table(path: Path) -> tuple[_TableEntry, ...]:
    text = path.read_text(encoding="utf-8-sig")
    entries: list[_TableEntry] = []
    for form in _balanced_lib_forms(text):
        fields = {
            match.group("key"): _unescape_table_value(match.group("value"))
            for match in _FIELD_RE.finditer(form)
        }
        if fields.get("name") and fields.get("uri"):
            entries.append(
                _TableEntry(
                    name=fields["name"],
                    table_type=fields.get("type", "KiCad"),
                    uri=fields["uri"],
                    description=fields.get("descr", ""),
                    source_table=path,
                )
            )
    return tuple(entries)


def _expand_uri(uri: str, environment: Mapping[str, str], table: Path) -> Path:
    expanded = _ENV_RE.sub(lambda match: environment.get(match.group(1), match.group(0)), uri)
    candidate = Path(os.path.expandvars(expanded)).expanduser()
    if not candidate.is_absolute():
        candidate = table.parent / candidate
    return candidate.resolve()


def _top_level_symbol_names(path: Path) -> tuple[str, ...]:
    """Extract top-level symbol names while ignoring nested graphical units."""

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    matches = tuple(
        (
            len(match.group("indent").expandtabs(4)),
            _unescape_table_value(match.group("name")),
        )
        for match in re.finditer(
            r'(?m)^(?P<indent>[ \t]+)\(symbol\s+"(?P<name>(?:\\.|[^"\\])*)"',
            text,
        )
    )
    if not matches:
        return ()
    top_level_indent = min(indent for indent, _name in matches)
    return tuple(name for indent, name in matches if indent == top_level_indent)


def _balanced_form(text: str, start: int) -> str:
    depth = 0
    quoted = False
    escaped = False
    for cursor in range(start, len(text)):
        char = text[cursor]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : cursor + 1]
    raise ValueError("unterminated KiCad s-expression")


def _selected_source(summary: LibraryPartSummary, source: str) -> str:
    if summary.kind is LibraryKind.FOOTPRINT:
        return source
    matches = tuple(
        re.finditer(
            r'(?m)^(?P<indent>[ \t]+)\(symbol\s+"(?P<name>(?:\\.|[^"\\])*)"',
            source,
        )
    )
    if not matches:
        raise ValueError(f"symbol {summary.identifier!r} was not found in its source library")
    top_level_indent = min(len(match.group("indent").expandtabs(4)) for match in matches)
    for match in matches:
        if (
            len(match.group("indent").expandtabs(4)) == top_level_indent
            and _unescape_table_value(match.group("name")) == summary.name
        ):
            return _balanced_form(source, match.start()).strip()
    raise ValueError(f"symbol {summary.identifier!r} was not found in its source library")


def _default_environment(kicad_cli: Path | None) -> dict[str, str]:
    environment = dict(os.environ)
    if kicad_cli is None:
        return environment
    share = kicad_cli.parent.parent / "share" / "kicad"
    environment.setdefault("KICAD10_SYMBOL_DIR", str(share / "symbols"))
    environment.setdefault("KICAD10_FOOTPRINT_DIR", str(share / "footprints"))
    environment.setdefault("KICAD10_3DMODEL_DIR", str(share / "3dmodels"))
    environment.setdefault("KICAD10_TEMPLATE_DIR", str(share / "template"))
    return environment


def default_library_tables(kicad_cli: Path | None = None) -> tuple[Path, ...]:
    """Return user tables when present, otherwise the installed template tables."""

    cli = kicad_cli or discover_tools().kicad_cli
    candidates: list[Path] = []
    appdata = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None
    if appdata is not None:
        candidates.extend(
            [
                appdata / "kicad" / "10.0" / "sym-lib-table",
                appdata / "kicad" / "10.0" / "fp-lib-table",
            ]
        )
    if cli is not None:
        template = cli.parent.parent / "share" / "kicad" / "template"
        candidates.extend([template / "sym-lib-table", template / "fp-lib-table"])
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


class LibraryCatalog:
    """SQLite-backed index over reachable native KiCad libraries."""

    def __init__(
        self,
        database_path: Path,
        *,
        table_paths: Iterable[Path] | None = None,
        kicad_cli: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.kicad_cli = kicad_cli if kicad_cli is not None else discover_tools().kicad_cli
        self.environment = dict(environment or _default_environment(self.kicad_cli))
        self.table_paths = tuple(table_paths or default_library_tables(self.kicad_cli))
        self._diagnostics: list[LibraryDiagnostic] = []
        self._initialize()

    @property
    def diagnostics(self) -> tuple[LibraryDiagnostic, ...]:
        return tuple(self._diagnostics)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_parts (
                    identifier TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    library_name TEXT NOT NULL,
                    part_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    description TEXT NOT NULL,
                    table_type TEXT NOT NULL,
                    native_supported INTEGER NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_search
                    ON catalog_parts(kind, library_name, part_name);
                """
            )

    def refresh(self) -> CatalogRefreshReport:
        diagnostics: list[LibraryDiagnostic] = []
        reachable = self._resolve_tables(diagnostics)
        rows_by_identifier: dict[str, tuple[object, ...]] = {}
        library_count = 0
        for entry, path in reachable:
            library_count += 1
            if entry.table_type.casefold() != "kicad":
                diagnostics.append(
                    LibraryDiagnostic(
                        "LIBRARY_TYPE_UNSUPPORTED",
                        f"Library {entry.name!r} uses unsupported type {entry.table_type!r}.",
                        str(path),
                    )
                )
                continue
            if not path.exists():
                diagnostics.append(
                    LibraryDiagnostic(
                        "LIBRARY_PATH_MISSING",
                        f"Library {entry.name!r} could not be resolved.",
                        str(path),
                    )
                )
                continue
            try:
                if path.suffix.casefold() == ".kicad_sym":
                    stat = path.stat()
                    for name in _top_level_symbol_names(path):
                        identifier = f"{entry.name}:{name}"
                        rows_by_identifier.setdefault(
                            identifier,
                            (
                                identifier,
                                LibraryKind.SYMBOL.value,
                                entry.name,
                                name,
                                str(path),
                                entry.description,
                                entry.table_type,
                                1,
                                stat.st_size,
                                stat.st_mtime_ns,
                            ),
                        )
                elif path.is_dir() and path.suffix.casefold() == ".pretty":
                    for footprint in sorted(path.glob("*.kicad_mod")):
                        stat = footprint.stat()
                        identifier = f"{entry.name}:{footprint.stem}"
                        rows_by_identifier.setdefault(
                            identifier,
                            (
                                identifier,
                                LibraryKind.FOOTPRINT.value,
                                entry.name,
                                footprint.stem,
                                str(footprint.resolve()),
                                entry.description,
                                entry.table_type,
                                1,
                                stat.st_size,
                                stat.st_mtime_ns,
                            ),
                        )
                else:
                    diagnostics.append(
                        LibraryDiagnostic(
                            "LIBRARY_FORMAT_UNSUPPORTED",
                            f"Library {entry.name!r} is not a native KiCad symbol "
                            "or footprint library.",
                            str(path),
                        )
                    )
            except OSError as error:
                diagnostics.append(
                    LibraryDiagnostic(
                        "LIBRARY_READ_FAILED",
                        f"Library {entry.name!r} could not be indexed: {error}",
                        str(path),
                    )
                )

        rows = list(rows_by_identifier.values())
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM catalog_parts")
            connection.executemany(
                """
                INSERT INTO catalog_parts (
                    identifier, kind, library_name, part_name, source_path,
                    description, table_type, native_supported,
                    source_size, source_mtime_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
        self._diagnostics = diagnostics
        symbol_count = sum(row[1] == LibraryKind.SYMBOL.value for row in rows)
        footprint_count = sum(row[1] == LibraryKind.FOOTPRINT.value for row in rows)
        return CatalogRefreshReport(
            symbol_count=symbol_count,
            footprint_count=footprint_count,
            library_count=library_count,
            diagnostics=tuple(diagnostics),
        )

    def _resolve_tables(
        self, diagnostics: list[LibraryDiagnostic]
    ) -> tuple[tuple[_TableEntry, Path], ...]:
        queue = list(self.table_paths)
        visited: set[Path] = set()
        resolved: list[tuple[_TableEntry, Path]] = []
        while queue:
            table = queue.pop(0).resolve()
            if table in visited:
                continue
            visited.add(table)
            if not table.is_file():
                diagnostics.append(
                    LibraryDiagnostic(
                        "LIBRARY_TABLE_MISSING", "KiCad library table is missing.", str(table)
                    )
                )
                continue
            try:
                entries = _parse_table(table)
            except (OSError, UnicodeError) as error:
                diagnostics.append(
                    LibraryDiagnostic(
                        "LIBRARY_TABLE_INVALID",
                        f"KiCad library table could not be read: {error}",
                        str(table),
                    )
                )
                continue
            for entry in entries:
                path = _expand_uri(entry.uri, self.environment, table)
                if entry.table_type.casefold() == "table":
                    queue.append(path)
                else:
                    resolved.append((entry, path))
        return tuple(resolved)

    def search(
        self,
        query: str = "",
        *,
        kind: LibraryKind | None = None,
        limit: int = 200,
    ) -> tuple[LibraryPartSummary, ...]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        term = f"%{query.strip().casefold()}%"
        clauses = ["(lower(identifier) LIKE ? OR lower(description) LIKE ?)"]
        parameters: list[object] = [term, term]
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind.value)
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT identifier, kind, library_name, part_name, source_path,
                       description, table_type, native_supported
                  FROM catalog_parts
                 WHERE {" AND ".join(clauses)}
                 ORDER BY lower(library_name), lower(part_name)
                 LIMIT ?
                """,  # noqa: S608 - only fixed internal clauses are interpolated
                parameters,
            ).fetchall()
        return tuple(self._summary(row) for row in rows)

    def get(self, identifier: str) -> LibraryPartSummary | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT identifier, kind, library_name, part_name, source_path,
                       description, table_type, native_supported
                  FROM catalog_parts WHERE identifier = ?
                """,
                (identifier,),
            ).fetchone()
        return self._summary(row) if row is not None else None

    @staticmethod
    def _summary(row: sqlite3.Row) -> LibraryPartSummary:
        return LibraryPartSummary(
            identifier=str(row["identifier"]),
            kind=LibraryKind(str(row["kind"])),
            library=str(row["library_name"]),
            name=str(row["part_name"]),
            source_path=str(row["source_path"]),
            description=str(row["description"]),
            table_type=str(row["table_type"]),
            native_supported=bool(row["native_supported"]),
        )

    def resolve(self, identifier: str) -> ResolvedLibraryAsset:
        summary = self.get(identifier)
        if summary is None:
            raise KeyError(identifier)
        path = Path(summary.source_path)
        library_source = path.read_text(encoding="utf-8-sig", errors="strict")
        raw_source = _selected_source(summary, library_source)
        source_hash = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        has_spice = any(marker in raw_source for marker in _SIMULATION_MARKERS)
        official = "share\\kicad" in str(path).casefold() or "/share/kicad" in str(path).casefold()
        internal_dc_model = (
            summary.kind is LibraryKind.SYMBOL and summary.identifier in _INTERNAL_DC_SYMBOLS
        )
        return ResolvedLibraryAsset(
            summary=summary,
            source_hash=source_hash,
            raw_source=raw_source,
            capabilities=LibraryCapabilities(
                render=self.kicad_cli is not None and self.kicad_cli.is_file(),
                # The alpha writer currently exports its owned resistor/source primitives.
                # Other native parts remain browseable and previewable without approximation.
                eda_export=internal_dc_model,
                internal_dc_model=internal_dc_model,
                ngspice_model=has_spice,
            ),
            license_id="CC-BY-SA-4.0 WITH KiCad-libraries-exception" if official else None,
            license_notice="KiCad official library asset; preserve source attribution."
            if official
            else None,
        )

    def snapshot(self, identifier: str) -> LibraryAssetSnapshot:
        """Capture one selected asset with provenance; never copy an entire library."""

        resolved = self.resolve(identifier)
        kind = (
            AssetKind.SYMBOL if resolved.summary.kind is LibraryKind.SYMBOL else AssetKind.FOOTPRINT
        )
        payload = {
            "schema_version": 1,
            "identifier": resolved.summary.identifier,
            "raw_s_expression": resolved.raw_source,
            "capabilities": {
                "render": resolved.capabilities.render,
                "eda_export": resolved.capabilities.eda_export,
                "internal_dc_model": resolved.capabilities.internal_dc_model,
                "ngspice_model": resolved.capabilities.ngspice_model,
            },
        }
        return LibraryAssetSnapshot(
            asset_id=resolved.summary.identifier,
            kind=kind,
            name=resolved.summary.name,
            source=resolved.summary.source_path,
            source_hash=resolved.source_hash,
            license_spdx=resolved.license_id or "NOASSERTION",
            payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def render_preview(
        self,
        identifier: str,
        output_directory: Path,
        *,
        timeout_s: float = 20.0,
    ) -> Path:
        summary = self.get(identifier)
        if summary is None:
            raise KeyError(identifier)
        if self.kicad_cli is None or not self.kicad_cli.is_file():
            raise FileNotFoundError("kicad-cli is required to render library previews")
        output_directory = Path(output_directory)
        output_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="smd-eda-preview-") as temporary:
            temporary_path = Path(temporary)
            if summary.kind is LibraryKind.SYMBOL:
                argv = (
                    self.kicad_cli,
                    "sym",
                    "export",
                    "svg",
                    "--symbol",
                    summary.name,
                    "--output",
                    temporary_path,
                    summary.source_path,
                )
            else:
                source = Path(summary.source_path)
                argv = (
                    self.kicad_cli,
                    "fp",
                    "export",
                    "svg",
                    "--footprint",
                    summary.name,
                    "--output",
                    temporary_path,
                    source.parent,
                )
            result = run_isolated_process(argv, timeout_s=timeout_s)
            if result.timed_out:
                raise TimeoutError("KiCad preview rendering timed out")
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                raise RuntimeError(f"KiCad preview rendering failed: {detail}")
            candidates = sorted(temporary_path.rglob("*.svg"))
            if not candidates:
                raise RuntimeError("KiCad did not produce an SVG preview")
            digest = hashlib.sha256(identifier.encode()).hexdigest()[:16]
            destination = output_directory / f"{digest}.svg"
            destination.write_bytes(candidates[0].read_bytes())
            return destination
