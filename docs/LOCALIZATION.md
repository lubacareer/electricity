# Localization

The desktop UI currently supports English (`en`) and Russian (`ru`). English
fallback text stays beside each stable message ID in Python. Russian
translations live in the editable
`translations/smd_twin_lab_ru.ts` catalog, and the application loads the
compiled `src/smd_twin_lab/resources/i18n/smd_twin_lab_ru.qm` file. Lessons are
Markdown files under `resources/lessons/<language>/`; a missing translated
lesson falls back to English.

## Update the Russian catalog

1. Add or update the stable message ID and its English fallback in the code.
2. Add the same ID, source text, and Russian translation to the TS file. Keep
   every named placeholder, such as `{reference}`, unchanged. For plural
   messages, keep `numerus="yes"`, `%n`, and all three Russian forms.
3. Compile the ID-based catalog from the repository root:

   ```powershell
   .\.venv\Scripts\pyside6-lrelease.exe -idbased -fail-on-unfinished -fail-on-invalid translations\smd_twin_lab_ru.ts -qm src\smd_twin_lab\resources\i18n\smd_twin_lab_ru.qm
   ```

4. Run the catalog and packaging checks:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest tests\test_localization.py tests\test_translation_catalog.py tests\test_packaging.py
   ```

`pyside6-lupdate` can merge strings written with Qt's supported translation
macros or `.ui` forms into a TS file. The current Python API uses explicit
stable IDs, so the catalog-completeness test is the authoritative check for
those entries; do not run `lupdate -no-obsolete` over this catalog unless all
stable-ID messages are also exposed through supported extraction macros.

## Add another language

1. Add one `LanguageSpec` entry in `localization.py` with the language code,
   native display name, Qt locale, and QM filename.
2. Copy the Russian TS structure to a new locale-named TS file, translate it,
   and compile an ID-based QM into `resources/i18n/`.
3. Add translated lessons under `resources/lessons/<code>/`. Keep the English
   lesson with the same filename so fallback remains available.
4. Extend the catalog/resource tests and exercise an in-window language switch.

Project names, component references, net names, paths, raw UART, measurement
keys, enum values, and external-tool output are technical data and should not
be translated.
