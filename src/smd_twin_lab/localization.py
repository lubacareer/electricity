"""Application localization and deferred message rendering.

The GUI owns one :class:`LanguageManager`. Background report/history code uses
``CatalogRenderer`` instead, so rendering a saved report never changes the
process-wide Qt language.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6 import QtCore

from .models import MessageRef

SETTINGS_KEY = "ui/language"
TRANSLATION_CONTEXT = "smd_twin_lab"


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    code: str
    native_name: str
    qt_locale: str
    catalog_name: str


SUPPORTED_LANGUAGES = (
    LanguageSpec("en", "English", "en_US", ""),
    LanguageSpec("ru", "Русский", "ru_RU", "smd_twin_lab_ru.qm"),
)
LANGUAGES_BY_CODE = {language.code: language for language in SUPPORTED_LANGUAGES}


def _default_catalog_directory() -> Path:
    return Path(__file__).with_name("resources") / "i18n"


def _language_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    primary = value.strip().replace("-", "_").partition("_")[0].lower()
    return primary if primary in LANGUAGES_BY_CODE else None


def _system_language(ui_languages: Iterable[str]) -> str:
    return "ru" if any(_language_code(language) == "ru" for language in ui_languages) else "en"


def _load_application_translator(
    language: LanguageSpec,
    catalog_directory: Path,
    parent: QtCore.QObject | None = None,
) -> QtCore.QTranslator | None:
    if not language.catalog_name:
        return None
    translator = QtCore.QTranslator(parent)
    if not translator.load(str(catalog_directory / language.catalog_name)):
        return None
    return translator


def _load_qt_translator(
    language: LanguageSpec,
    parent: QtCore.QObject | None = None,
) -> QtCore.QTranslator | None:
    translator = QtCore.QTranslator(parent)
    translations_path = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.TranslationsPath)
    loaded = translator.load(
        QtCore.QLocale(language.qt_locale),
        "qtbase",
        "_",
        translations_path,
    )
    return translator if loaded else None


def _translated_template(
    translator: QtCore.QTranslator | None,
    message_id: str,
    count: int | None,
) -> str:
    if translator is None:
        return ""
    plural_count = count if count is not None else -1
    translated = translator.translate("", message_id, None, plural_count)
    if not translated:
        translated = translator.translate(
            TRANSLATION_CONTEXT,
            message_id,
            None,
            plural_count,
        )
    return translated


def _format_template(
    template: str,
    parameters: Mapping[str, Any] | None,
    count: int | None,
) -> str | None:
    values = dict(parameters or {})
    if count is not None:
        values.setdefault("count", count)
        template = template.replace("%n", str(count))
    try:
        return template.format_map(values)
    except (KeyError, ValueError, IndexError, AttributeError, TypeError):
        return None


def _render(
    translator: QtCore.QTranslator | None,
    message_id: str,
    fallback: str,
    parameters: Mapping[str, Any] | None,
    count: int | None,
) -> str:
    translated = _translated_template(translator, message_id, count)
    if translated:
        rendered = _format_template(translated, parameters, count)
        if rendered is not None:
            return rendered
    rendered_fallback = _format_template(fallback, parameters, count)
    return rendered_fallback if rendered_fallback is not None else fallback


class CatalogRenderer:
    """Render one message for an explicit language without global Qt changes."""

    def __init__(self, *, catalog_directory: Path | None = None) -> None:
        self._catalog_directory = (catalog_directory or _default_catalog_directory()).resolve()
        self._translators: dict[str, QtCore.QTranslator | None] = {}

    def _translator(self, language: str) -> QtCore.QTranslator | None:
        code = _language_code(language)
        if code is None or code == "en":
            return None
        if code not in self._translators:
            self._translators[code] = _load_application_translator(
                LANGUAGES_BY_CODE[code],
                self._catalog_directory,
            )
        return self._translators[code]

    def text(
        self,
        language: str,
        message_id: str,
        fallback: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        count: int | None = None,
    ) -> str:
        return _render(
            self._translator(language),
            message_id,
            fallback,
            parameters,
            count,
        )

    def render(
        self,
        language: str,
        message_ref: MessageRef | None,
        fallback: str,
    ) -> str:
        if message_ref is None:
            return fallback
        return self.text(
            language,
            message_ref.message_id,
            fallback,
            parameters=message_ref.parameters,
            count=message_ref.count,
        )


class LanguageManager(QtCore.QObject):
    """Own the live application's translators and selected language."""

    language_changed = QtCore.Signal(str)

    def __init__(
        self,
        application: QtCore.QCoreApplication | None = None,
        *,
        settings: QtCore.QSettings | None = None,
        catalog_directory: Path | None = None,
        system_ui_languages: Iterable[str] | None = None,
    ) -> None:
        super().__init__()
        self._application = application or QtCore.QCoreApplication.instance()
        self._settings = settings or QtCore.QSettings()
        self._catalog_directory = (catalog_directory or _default_catalog_directory()).resolve()
        self._current_language = "en"
        self._application_translator: QtCore.QTranslator | None = None
        self._qt_translator: QtCore.QTranslator | None = None

        stored = (
            self._settings.value(SETTINGS_KEY) if self._settings.contains(SETTINGS_KEY) else None
        )
        stored_code = _language_code(stored)
        if stored is not None:
            requested = stored_code or "en"
        else:
            detected_languages = (
                tuple(system_ui_languages)
                if system_ui_languages is not None
                else QtCore.QLocale.system().uiLanguages()
            )
            requested = _system_language(detected_languages)

        if requested != "en" and not self._activate(requested):
            requested = "en"
        self._current_language = requested
        if stored is not None and stored_code != requested:
            self._persist(requested)

    @property
    def available_languages(self) -> tuple[LanguageSpec, ...]:
        return SUPPORTED_LANGUAGES

    @property
    def current_language(self) -> str:
        return self._current_language

    def set_language(self, code: str) -> bool:
        normalized = _language_code(code)
        if normalized is None:
            return False
        if normalized == self._current_language:
            self._persist(normalized)
            return True
        if not self._activate(normalized):
            return False
        self._current_language = normalized
        self._persist(normalized)
        self.language_changed.emit(normalized)
        return True

    def text(
        self,
        message_id: str,
        fallback: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        count: int | None = None,
    ) -> str:
        return _render(
            self._application_translator,
            message_id,
            fallback,
            parameters,
            count,
        )

    def render(self, message_ref: MessageRef | None, fallback: str) -> str:
        if message_ref is None:
            return fallback
        return self.text(
            message_ref.message_id,
            fallback,
            parameters=message_ref.parameters,
            count=message_ref.count,
        )

    def _activate(self, code: str) -> bool:
        if code == "en":
            self._remove_active_translators()
            return True

        language = LANGUAGES_BY_CODE[code]
        application_translator = _load_application_translator(
            language,
            self._catalog_directory,
            self,
        )
        if application_translator is None:
            return False
        qt_translator = _load_qt_translator(language, self)
        if qt_translator is None:
            return False

        if self._application is not None:
            if not self._application.installTranslator(qt_translator):
                return False
            if not self._application.installTranslator(application_translator):
                self._application.removeTranslator(qt_translator)
                return False
        self._remove_active_translators()
        self._qt_translator = qt_translator
        self._application_translator = application_translator
        return True

    def _remove_active_translators(self) -> None:
        if self._application is not None:
            if self._application_translator is not None:
                self._application.removeTranslator(self._application_translator)
            if self._qt_translator is not None:
                self._application.removeTranslator(self._qt_translator)
        self._application_translator = None
        self._qt_translator = None

    def _persist(self, code: str) -> None:
        self._settings.setValue(SETTINGS_KEY, code)
        self._settings.sync()

    def close(self) -> None:
        """Remove translators installed by this manager."""

        self._remove_active_translators()


_current_manager: LanguageManager | None = None


def initialize_language_manager(
    application: QtCore.QCoreApplication | None = None,
    *,
    settings: QtCore.QSettings | None = None,
    system_languages: Iterable[str] | None = None,
    catalog_directory: Path | None = None,
) -> LanguageManager:
    """Create and register the process-wide manager used by the desktop UI."""

    global _current_manager
    if _current_manager is not None:
        _current_manager.close()
    _current_manager = LanguageManager(
        application,
        settings=settings,
        catalog_directory=catalog_directory,
        system_ui_languages=system_languages,
    )
    return _current_manager


def current_language_manager() -> LanguageManager:
    """Return the desktop manager, lazily creating it for embedders/tests."""

    global _current_manager
    if _current_manager is None:
        _current_manager = LanguageManager()
    return _current_manager
