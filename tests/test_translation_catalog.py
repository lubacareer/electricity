from __future__ import annotations

import ast
import re
import string
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from PySide6 import QtCore

from smd_twin_lab.localization import SETTINGS_KEY, CatalogRenderer, LanguageManager
from smd_twin_lab.models import MessageRef

ROOT = Path(__file__).parents[1]
TS_PATH = ROOT / "translations" / "smd_twin_lab_ru.ts"
QM_PATH = ROOT / "src" / "smd_twin_lab" / "resources" / "i18n" / "smd_twin_lab_ru.qm"
EDA_ISSUE_PATHS = tuple(
    ROOT / "src" / "smd_twin_lab" / "eda" / filename
    for filename in ("connectivity.py", "pcb.py", "simulation.py")
)

_ID_PATTERN = re.compile(
    r"^(?:main|designer|board|component|waveform|teaching|capability|diagnostic|explanation)"
    r"\.[a-z0-9_.-]+$"
)
_DYNAMIC_ID_FAMILIES = {
    "main.component_side.": ("front", "back", "unknown"),
    "main.diagnostic_severity.": ("info", "warning", "error"),
    "main.diagnostics.count.": ("info", "warning", "error"),
    "main.firmware_state.": ("normal", "alarm", "sensor_fault"),
    "component.side.": ("front", "back", "unknown"),
}
_NUMERUS_IDS = {
    "capability.kicad.geometry_imported",
    "diagnostic.bundle.geometry_assets_missing",
    "diagnostic.kicad.sibling_ambiguous",
    "main.count.components",
    "main.count.nets",
    "main.count.pins",
    "main.diagnostics.count.error",
    "main.diagnostics.count.info",
    "main.diagnostics.count.warning",
    "main.designer.export_complete",
    "main.inspector.connected_pins",
}


def _source_message_ids() -> set[str]:
    message_ids: set[str] = set()
    for path in (ROOT / "src" / "smd_twin_lab").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if _ID_PATTERN.fullmatch(node.value) and not node.value.endswith("."):
                message_ids.add(node.value)
    for prefix, suffixes in _DYNAMIC_ID_FAMILIES.items():
        message_ids.update(prefix + suffix for suffix in suffixes)
    return message_ids


def _catalog_messages() -> dict[str, ElementTree.Element]:
    root = ElementTree.parse(TS_PATH).getroot()
    messages = root.findall(".//message")
    message_ids = [message.attrib["id"] for message in messages]
    assert len(message_ids) == len(set(message_ids)), "translation IDs must be unique"
    return dict(zip(message_ids, messages, strict=True))


def _placeholders(text: str) -> set[str]:
    placeholders: set[str] = set()
    for _, field_name, _, _ in string.Formatter().parse(text):
        if field_name:
            placeholders.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return placeholders


def test_russian_catalog_covers_every_source_message_id() -> None:
    assert set(_catalog_messages()) == _source_message_ids()


def test_designer_issue_messages_cover_every_first_party_issue_code() -> None:
    issue_codes: set[str] = set()
    for path in EDA_ISSUE_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        issue_codes.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(("erc.", "drc.", "pcb.", "router.", "circuit."))
        )

    expected_ids = {f"designer.issue.{code}" for code in issue_codes}
    actual_ids = {
        message_id for message_id in _catalog_messages() if message_id.startswith("designer.issue.")
    }
    assert actual_ids == expected_ids


def test_russian_catalog_is_finished_and_preserves_placeholders() -> None:
    for message_id, message in _catalog_messages().items():
        source = message.findtext("source", default="")
        translation = message.find("translation")
        assert translation is not None, message_id
        assert translation.attrib.get("type") != "unfinished", message_id

        if message.attrib.get("numerus") == "yes":
            forms = [item.text or "" for item in translation.findall("numerusform")]
            assert len(forms) == 3, message_id
            assert all(form.strip() for form in forms), message_id
        else:
            forms = [translation.text or ""]
            assert forms[0].strip(), message_id

        source_placeholders = _placeholders(source)
        for form in forms:
            assert _placeholders(form) == source_placeholders, message_id


def test_every_count_message_has_three_russian_plural_forms() -> None:
    messages = _catalog_messages()
    actual_numerus_ids = {
        message_id
        for message_id, message in messages.items()
        if message.attrib.get("numerus") == "yes"
    }
    assert actual_numerus_ids == _NUMERUS_IDS


def test_compiled_catalog_loads_and_renders_ids_and_plurals() -> None:
    translator = QtCore.QTranslator()
    assert translator.load(str(QM_PATH))
    assert not translator.isEmpty()

    renderer = CatalogRenderer()
    assert renderer.render("ru", MessageRef("main.menu.language"), "Language") == "Язык"
    assert (
        renderer.render("ru", MessageRef("designer.tab.schematic"), "Schematic")
        == "Принципиальная схема"
    )
    assert (
        renderer.render(
            "ru",
            MessageRef("designer.issue.circuit.missing_ground"),
            "The circuit needs a net labelled GND or 0.",
        )
        == "Схеме нужна цепь с меткой GND или 0."
    )
    assert (
        renderer.render(
            "ru",
            MessageRef(
                "main.designer.simulation_result",
                parameters={"engine": "ngspice", "probe": "VOUT", "voltage": "1.65"},
            ),
            "{engine}: {probe} = {voltage} V",
        )
        == "ngspice: VOUT = 1.65 В"
    )
    expected_forms = {
        1: "1 компонент",
        2: "2 компонента",
        5: "5 компонентов",
    }
    for count, expected in expected_forms.items():
        assert (
            renderer.render(
                "ru",
                MessageRef("main.count.components", count=count),
                "{count} components",
            )
            == expected
        )

    export_forms = {
        1: "Проверка в KiCad пройдена; создан 1 производственный файл.",
        2: "Проверка в KiCad пройдена; создано 2 производственных файла.",
        5: "Проверка в KiCad пройдена; создано 5 производственных файлов.",
    }
    for count, expected in export_forms.items():
        assert (
            renderer.render(
                "ru",
                MessageRef("main.designer.export_complete", count=count),
                "KiCad verification passed; {count} manufacturing files were created.",
            )
            == expected
        )


def test_real_catalog_first_launch_detection_and_persistence(qapp, tmp_path: Path) -> None:
    settings = QtCore.QSettings(
        str(tmp_path / "settings.ini"),
        QtCore.QSettings.Format.IniFormat,
    )
    first = LanguageManager(
        qapp,
        settings=settings,
        system_ui_languages=("ru-RU", "en-US"),
    )
    try:
        assert first.current_language == "ru"
        assert settings.value(SETTINGS_KEY) is None
        assert first.set_language("ru")
        assert settings.value(SETTINGS_KEY) == "ru"
    finally:
        first.close()

    restored = LanguageManager(
        qapp,
        settings=settings,
        system_ui_languages=("en-US",),
    )
    try:
        assert restored.current_language == "ru"
    finally:
        restored.close()
