from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

from smd_twin_lab.eda.kicad import _semantic_netlist_summary
from smd_twin_lab.importers.parsers import ArtifactParseError, parse_logical_netlist, validate_svg


@pytest.mark.parametrize("entity", ['"expanded"', 'SYSTEM "file:///nonexistent"'])
def test_xml_parsers_reject_entities(tmp_path: Path, entity: str) -> None:
    path = tmp_path / "untrusted.xml"
    path.write_text(
        f"<!DOCTYPE export [<!ENTITY attack {entity}>]>"
        '<export><nets><net name="&attack;"/></nets></export>',
        encoding="utf-8",
    )
    with pytest.raises(ArtifactParseError):
        parse_logical_netlist(path)
    with pytest.raises(DefusedXmlException):
        _semantic_netlist_summary(path)

    path.write_text(
        f"<!DOCTYPE svg [<!ENTITY attack {entity}>]><svg>&attack;</svg>", encoding="utf-8"
    )
    with pytest.raises(ArtifactParseError):
        validate_svg(path)


def test_ordinary_xml_still_parses(tmp_path: Path) -> None:
    path = tmp_path / "netlist.xml"
    path.write_text('<export><nets><net code="1" name="GND"/></nets></export>', encoding="utf-8")
    nets, _ = parse_logical_netlist(path)
    assert len(nets) == 1
    assert _semantic_netlist_summary(path) == ((), ("GND",))
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    validate_svg(path)


def test_harmless_doctypes_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "netlist.xml"
    path.write_text('<!DOCTYPE export><export><nets><net name="GND"/></nets></export>')
    assert len(parse_logical_netlist(path)[0]) == 1
    assert _semantic_netlist_summary(path) == ((), ("GND",))
    path.write_text('<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>')
    validate_svg(path)
