"""Read-only project importers and normalized bundle loading."""

from .bundle import BUNDLE_FILENAME, load_bundle
from .kicad import KiCadProjectImporter

# A short alias is convenient in callers while the longer name remains explicit.
KiCadImporter = KiCadProjectImporter

__all__ = [
    "BUNDLE_FILENAME",
    "KiCadImporter",
    "KiCadProjectImporter",
    "load_bundle",
]
