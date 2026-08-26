from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6 import QtCore

import smd_twin_lab.localization as localization
import smd_twin_lab.ui.teaching as teaching
from smd_twin_lab.models import (
    Capability,
    CapabilityStatus,
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    FaultSpec,
    FirmwareState,
    MessageRef,
    RunReport,
    Scenario,
)


class StubTranslator(QtCore.QTranslator):
    def __init__(self, messages: dict[str, str] | None = None) -> None:
        super().__init__()
        self.messages = messages or {}

    def translate(
        self,
        context: str,
        source_text: str,
        disambiguation: str | None = None,
        n: int = -1,
    ) -> str:
        del context, disambiguation, n
        return self.messages.get(source_text, "")


class RussianPluralTranslator(StubTranslator):
    def translate(
        self,
        context: str,
        source_text: str,
        disambiguation: str | None = None,
        n: int = -1,
    ) -> str:
        del context, disambiguation
        if source_text != "browser.component_count":
            return ""
        if n % 10 == 1 and n % 100 != 11:
            return "{count} компонент"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "{count} компонента"
        return "{count} компонентов"


def _settings(path: Path) -> QtCore.QSettings:
    return QtCore.QSettings(str(path), QtCore.QSettings.Format.IniFormat)


def test_message_ref_is_json_safe_and_model_additions_are_compatible() -> None:
    message_ref = MessageRef("diagnostic.example", {"reference": "R2"})
    assert message_ref.parameters == {"reference": "R2"}
    assert Capability(CapabilityStatus.AVAILABLE, "Ready").message_ref is None
    assert Diagnostic(DiagnosticSeverity.INFO, "ready", "Ready").message_ref is None
    assert Scenario("id", "name", 25.0, FaultSpec(FaultKind.NONE)).language == "en"

    with pytest.raises(TypeError, match="JSON-safe"):
        MessageRef("bad", {"value": object()})
    with pytest.raises(ValueError, match="must not be empty"):
        MessageRef(" ")


def test_message_ref_deep_copies_nested_parameters() -> None:
    references = ["R2"]
    metadata = {"unit": "ohm"}
    parameters = {"context": {"references": references, "metadata": metadata}}

    message_ref = MessageRef("diagnostic.example", parameters)
    references.append("R3")
    metadata["unit"] = "kohm"

    assert type(message_ref.parameters) is dict
    assert message_ref.parameters == {
        "context": {"references": ["R2"], "metadata": {"unit": "ohm"}}
    }


def test_canonical_report_omits_empty_compatibility_metadata() -> None:
    timestamp = datetime.now(UTC).isoformat()
    report = RunReport(
        schema_version=1,
        run_id="run-1",
        project_id="board-1",
        scenario_id="nominal",
        started_at=timestamp,
        completed_at=timestamp,
        passed=True,
        infrastructure_error=False,
        firmware_state=FirmwareState.NORMAL,
        outputs={},
        measurements={},
        signals=(),
        timeline=(),
        explanations=(),
        diagnostics=(Diagnostic(DiagnosticSeverity.INFO, "ready", "Ready"),),
    )

    payload = report.to_dict()
    assert "explanation_refs" not in payload
    assert "message_ref" not in payload["diagnostics"][0]


def test_first_run_language_detection_and_missing_catalog_fallback(
    qapp,
    tmp_path: Path,
) -> None:
    english = localization.LanguageManager(
        qapp,
        settings=_settings(tmp_path / "english.ini"),
        catalog_directory=tmp_path,
        system_ui_languages=("en-US",),
    )
    russian_without_catalog = localization.LanguageManager(
        qapp,
        settings=_settings(tmp_path / "russian.ini"),
        catalog_directory=tmp_path,
        system_ui_languages=("ru-RU", "en-US"),
    )
    try:
        assert english.current_language == "en"
        assert russian_without_catalog.current_language == "en"
        assert [item.code for item in english.available_languages] == ["en", "ru"]
    finally:
        english.close()
        russian_without_catalog.close()


def test_live_language_change_persists_and_formats_messages(
    qapp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_translator = StubTranslator(
        {
            "browser.component_count": "{count} компонента",
            "message.invalid_format": "{missing}",
        }
    )
    monkeypatch.setattr(
        localization,
        "_load_application_translator",
        lambda *args, **kwargs: app_translator,
    )
    monkeypatch.setattr(
        localization,
        "_load_qt_translator",
        lambda *args, **kwargs: StubTranslator(),
    )
    settings = _settings(tmp_path / "settings.ini")
    manager = localization.LanguageManager(
        qapp,
        settings=settings,
        system_ui_languages=("en-US",),
    )
    changes: list[str] = []
    manager.language_changed.connect(changes.append)
    try:
        assert manager.set_language("ru")
        assert manager.current_language == "ru"
        assert settings.value(localization.SETTINGS_KEY) == "ru"
        assert changes == ["ru"]
        assert (
            manager.text(
                "browser.component_count",
                "{count} components",
                count=2,
            )
            == "2 компонента"
        )
        assert (
            manager.render(
                MessageRef("message.invalid_format", {"value": "R2"}),
                "Component {value}",
            )
            == "Component R2"
        )

        assert manager.set_language("en")
        assert manager.current_language == "en"
        assert changes == ["ru", "en"]
        assert manager.render(None, "Unstructured third-party text") == (
            "Unstructured third-party text"
        )
        assert manager.text("missing", "Value {value}", parameters={"value": 3}) == "Value 3"
    finally:
        manager.close()


def test_failed_switch_is_atomic_and_invalid_setting_is_repaired(
    qapp,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "settings.ini")
    settings.setValue(localization.SETTINGS_KEY, "unsupported")
    manager = localization.LanguageManager(
        qapp,
        settings=settings,
        catalog_directory=tmp_path,
        system_ui_languages=("ru-RU",),
    )
    try:
        assert manager.current_language == "en"
        assert settings.value(localization.SETTINGS_KEY) == "en"
        assert not manager.set_language("ru")
        assert manager.current_language == "en"
        assert settings.value(localization.SETTINGS_KEY) == "en"
        assert not manager.set_language("de")
    finally:
        manager.close()


def test_catalog_renderer_does_not_install_global_translators(
    qapp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = QtCore.QCoreApplication.translate("", "report.result")
    monkeypatch.setattr(
        localization,
        "_load_application_translator",
        lambda *args, **kwargs: StubTranslator({"report.result": "Результат: {result}"}),
    )
    renderer = localization.CatalogRenderer(catalog_directory=tmp_path)
    message_ref = MessageRef("report.result", {"result": "PASS"})

    assert renderer.render("ru", message_ref, "Result: {result}") == "Результат: PASS"
    assert renderer.render("en", message_ref, "Result: {result}") == "Result: PASS"
    assert renderer.render("de", message_ref, "Result: {result}") == "Result: PASS"
    assert QtCore.QCoreApplication.translate("", "report.result") == before


def test_count_is_forwarded_for_russian_plural_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        localization,
        "_load_application_translator",
        lambda *args, **kwargs: RussianPluralTranslator(),
    )
    renderer = localization.CatalogRenderer(catalog_directory=tmp_path)

    assert renderer.text("ru", "browser.component_count", "{count} components", count=1) == (
        "1 компонент"
    )
    assert renderer.text("ru", "browser.component_count", "{count} components", count=2) == (
        "2 компонента"
    )
    assert renderer.text("ru", "browser.component_count", "{count} components", count=5) == (
        "5 компонентов"
    )


def test_lesson_loader_uses_any_registered_locale_with_per_topic_english_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    english = tmp_path / "resources" / "lessons" / "en"
    translated = tmp_path / "resources" / "lessons" / "xx"
    english.mkdir(parents=True)
    translated.mkdir(parents=True)
    (english / "getting_started.md").write_text("# English start", encoding="utf-8")
    (english / "aoi.md").write_text("# English AOI", encoding="utf-8")
    (translated / "getting_started.md").write_text("# Translated start", encoding="utf-8")
    monkeypatch.setattr(teaching.resources, "files", lambda _package: tmp_path)

    lessons = teaching.load_lessons("xx")

    assert lessons["getting_started"] == "# Translated start"
    assert lessons["aoi"] == "# English AOI"
