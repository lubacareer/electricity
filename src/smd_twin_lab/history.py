"""Versioned SQLite run history for local traceability."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RunReport

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    project_id: str
    scenario_id: str
    started_at: str
    completed_at: str
    passed: bool
    infrastructure_error: bool
    firmware_state: str


class RunHistory:
    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_info "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), version INTEGER NOT NULL)"
            )
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
            version = int(row["version"]) if row else 0
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"run-history schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                connection.execute(
                    """CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        scenario_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        passed INTEGER NOT NULL,
                        infrastructure_error INTEGER NOT NULL,
                        firmware_state TEXT NOT NULL,
                        report_json TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "CREATE INDEX runs_project_started ON runs(project_id, started_at DESC)"
                )
                connection.execute(
                    "INSERT INTO schema_info(singleton, version) VALUES (1, 1) "
                    "ON CONFLICT(singleton) DO UPDATE SET version = excluded.version"
                )

    def save(self, report: RunReport) -> None:
        payload = json.dumps(report.to_dict(), separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs(
                    run_id, project_id, scenario_id, started_at, completed_at,
                    passed, infrastructure_error, firmware_state, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    scenario_id=excluded.scenario_id,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    passed=excluded.passed,
                    infrastructure_error=excluded.infrastructure_error,
                    firmware_state=excluded.firmware_state,
                    report_json=excluded.report_json""",
                (
                    report.run_id,
                    report.project_id,
                    report.scenario_id,
                    report.started_at,
                    report.completed_at,
                    int(report.passed),
                    int(report.infrastructure_error),
                    report.firmware_state.value,
                    payload,
                ),
            )

    def recent(self, project_id: str | None = None, limit: int = 50) -> tuple[RunSummary, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = (
            "SELECT run_id, project_id, scenario_id, started_at, completed_at, "
            "passed, infrastructure_error, firmware_state FROM runs"
        )
        parameters: tuple[Any, ...]
        if project_id is None:
            query += " ORDER BY started_at DESC LIMIT ?"
            parameters = (limit,)
        else:
            query += " WHERE project_id = ? ORDER BY started_at DESC LIMIT ?"
            parameters = (project_id, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            RunSummary(
                run_id=row["run_id"],
                project_id=row["project_id"],
                scenario_id=row["scenario_id"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                passed=bool(row["passed"]),
                infrastructure_error=bool(row["infrastructure_error"]),
                firmware_state=row["firmware_state"],
            )
            for row in rows
        )

    def load_json(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["report_json"]) if row else None
