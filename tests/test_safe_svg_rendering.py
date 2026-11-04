from __future__ import annotations

import base64
import json
import os
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from urllib.parse import quote_from_bytes

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem

from smd_twin_lab.importers.bundle import load_bundle
from smd_twin_lab.models import ComponentSide
from smd_twin_lab.ui import board_canvas
from smd_twin_lab.ui.safe_svg import MAX_TOTAL_BYTES, MAX_XML_NODES, UnsafeSvg, prepare_svg

SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
RED = '<rect width="10" height="10" fill="red"/>'
EVIL = (
    '<!DOCTYPE svg [<!ENTITY color "red">]>'
    + SVG_OPEN
    + '<rect width="10" height="10" fill="&color;"/></svg>'
).encode()


@pytest.fixture(scope="module")
def application() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pixel(data: bytes) -> QtGui.QColor:
    renderer = QSvgRenderer(QtCore.QByteArray(data))
    assert renderer.isValid()
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    return image.pixelColor(5, 5)


def _image_svg(href: str, attribute: str = "href") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10">'
        f'<image {attribute}="{href}" width="10" height="10"/></svg>'
    )


@pytest.mark.parametrize("attribute", ["href", "xlink:href"])
def test_relative_png_is_inlined_and_rendered(application, tmp_path: Path, attribute: str) -> None:
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "red.png"))
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("red.png", attribute), encoding="utf-8")
    assert _pixel(prepare_svg(path)) == QtGui.QColor("red")


@pytest.mark.parametrize("encoding", ["base64", "percent"])
def test_nested_clean_svg_data_uri_is_rendered(application, tmp_path: Path, encoding: str) -> None:
    inner = (SVG_OPEN + RED + "</svg>").encode()
    data = (
        ("base64," + base64.b64encode(inner).decode())
        if encoding == "base64"
        else ("," + quote_from_bytes(inner))
    )
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("data:image/svg+xml;" + data), encoding="utf-8")
    assert _pixel(prepare_svg(path)) == QtGui.QColor("red")


@pytest.mark.parametrize("mime", ["image/svg+xml", "image/png", "text/plain"])
@pytest.mark.parametrize("encoding", ["base64", "percent"])
def test_nested_entities_rejected_despite_mime(application, tmp_path: Path, mime, encoding) -> None:
    data = (
        (";base64," + base64.b64encode(EVIL).decode())
        if encoding == "base64"
        else ("," + quote_from_bytes(EVIL))
    )
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("data:" + mime + data), encoding="utf-8")
    with pytest.raises(UnsafeSvg):
        prepare_svg(path)


def test_relative_nested_svg_entity_is_rejected(application, tmp_path: Path) -> None:
    (tmp_path / "nested.svg").write_bytes(EVIL)
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("nested.svg"), encoding="utf-8")
    with pytest.raises(UnsafeSvg):
        prepare_svg(path)


def test_safe_relative_svg_is_rendered(application, tmp_path: Path) -> None:
    (tmp_path / "nested.svg").write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("nested.svg"), encoding="utf-8")
    assert _pixel(prepare_svg(path)) == QtGui.QColor("red")


@pytest.mark.parametrize(
    "href",
    [
        "https://example.test/a.png",
        "http://[",
        "../outside.png",
        "%2e%2e/outside.png",
        "file:///a.png",
        "sub/../x.png",
    ],
)
def test_unsupported_image_paths_are_rejected(tmp_path: Path, href: str) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg(href), encoding="utf-8")
    with pytest.raises(UnsafeSvg):
        prepare_svg(path)


def test_rooted_local_path_is_rejected_even_within_directory(tmp_path: Path) -> None:
    image = tmp_path / "red.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    rooted = image.resolve().as_posix()
    if os.name == "nt":
        rooted = rooted[2:]
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg(rooted), encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="must stay within"):
        prepare_svg(path)


@pytest.mark.parametrize(
    "body",
    [
        "<script>alert(1)</script>",
        '<rect style="fill:url(https://example.test/x)"/>',
        '<rect style="fill:u\\72l(https://example.test/x)"/>',
        '<g xml:base="https://example.test/"/>',
    ],
)
def test_unsupported_elements_and_styles_are_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + body + "</svg>", encoding="utf-8")
    with pytest.raises(UnsafeSvg):
        prepare_svg(path)


def test_limits_apply_to_nested_resources(tmp_path: Path) -> None:
    path = tmp_path / "preview.svg"
    path.write_bytes(b" " * (MAX_TOTAL_BYTES + 1))
    with pytest.raises(UnsafeSvg, match="size limit"):
        prepare_svg(path)
    nested = (SVG_OPEN + RED + "</svg>").encode()
    for _ in range(6):
        nested = _image_svg(
            "data:image/svg+xml;base64," + base64.b64encode(nested).decode()
        ).encode()
    path.write_bytes(nested)
    with pytest.raises(UnsafeSvg, match="depth"):
        prepare_svg(path)
    path.write_text(SVG_OPEN + "<g>" * 140 + "</g>" * 140 + "</svg>", encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="complexity"):
        prepare_svg(path)
    path.write_text(SVG_OPEN + "<g/>" * MAX_XML_NODES + "</svg>", encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="complexity"):
        prepare_svg(path)


def test_raster_declared_dimensions_are_bounded(application, tmp_path: Path) -> None:
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "red.png"))
    data = bytearray((tmp_path / "red.png").read_bytes())
    data[16:24] = struct.pack(">II", 100_000, 100_000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    (tmp_path / "red.png").write_bytes(data)
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("red.png"), encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="dimensions|pixel"):
        prepare_svg(path)


def test_raster_pixel_limit_is_cumulative(application, tmp_path: Path) -> None:
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "red.png"))
    data = bytearray((tmp_path / "red.png").read_bytes())
    data[16:24] = struct.pack(">II", 3000, 3000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    (tmp_path / "red.png").write_bytes(data)
    path = tmp_path / "preview.svg"
    path.write_text(
        SVG_OPEN + '<image href="red.png"/><image href="red.png"/></svg>', encoding="utf-8"
    )
    with pytest.raises(UnsafeSvg, match="pixel"):
        prepare_svg(path)


def test_nested_svg_declared_dimensions_are_bounded(tmp_path: Path) -> None:
    nested = b'<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000"/>'
    path = tmp_path / "preview.svg"
    path.write_text(
        _image_svg("data:image/svg+xml;base64," + base64.b64encode(nested).decode()),
        encoding="utf-8",
    )
    with pytest.raises(UnsafeSvg, match="dimensions|pixel"):
        prepare_svg(path)


def test_unknown_xml_encoding_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "preview.svg"
    path.write_bytes(b'<?xml version="1.0" encoding="not-a-codec"?><svg/>')
    with pytest.raises(UnsafeSvg, match="invalid SVG XML"):
        prepare_svg(path)


@pytest.mark.parametrize(
    "attributes",
    [
        'width="1000000" height="1000000"',
        'viewBox="0 0 1000000 1000000"',
        'width="10" height="10" viewBox="0 0 1000000000000 10"',
        'width="1e300" height="1"',
    ],
)
def test_top_level_svg_dimensions_are_bounded(tmp_path: Path, attributes: str) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}/>', encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="dimensions|viewBox"):
        prepare_svg(path)


def test_kicad_physical_units_and_large_coordinate_viewbox_render(application, tmp_path: Path):
    path = tmp_path / "preview.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" '
        'viewBox="0 0 100000000 100000000">'
        '<rect width="100000000" height="100000000" fill="red"/></svg>',
        encoding="utf-8",
    )
    assert _pixel(prepare_svg(path)) == QtGui.QColor("red")


@pytest.mark.parametrize(
    "body",
    [
        '<use id="a" href="#a"/>',
        '<g id="a"><use href="#a"/></g>',
        '<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></defs>',
        '<defs><pattern id="a" href="#b"/><pattern id="b" href="#a"/></defs>',
        '<defs><pattern id="a"><rect fill="url(#b)"/></pattern>'
        '<pattern id="b"><rect fill="url(#a)"/></pattern></defs>',
    ],
)
def test_cyclic_svg_references_are_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + body + "</svg>", encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="cyclic"):
        prepare_svg(path)


def test_safe_internal_reference_renders(application, tmp_path: Path) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(
        SVG_OPEN + '<defs><g id="shape">' + RED + '</g></defs><use href="#shape"/></svg>',
        encoding="utf-8",
    )
    assert _pixel(prepare_svg(path)) == QtGui.QColor("red")


def test_acyclic_reference_expansion_is_bounded(tmp_path: Path) -> None:
    definitions = ['<g id="node0">' + RED + "</g>"]
    for i in range(1, 19):
        definitions.append(
            f'<g id="node{i}"><use href="#node{i - 1}"/><use href="#node{i - 1}"/></g>'
        )
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + "<defs>" + "".join(definitions) + "</defs></svg>", encoding="utf-8")
    with pytest.raises(UnsafeSvg, match="expansion"):
        prepare_svg(path)


def test_non_regular_opened_resource_is_rejected(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    original_fstat = os.fstat

    def fake_fstat(fd):
        result = original_fstat(fd)
        return os.stat_result((stat.S_IFIFO, *result[1:]))

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(UnsafeSvg, match="regular"):
        prepare_svg(path)


@pytest.mark.skipif(os.name != "posix", reason="FIFO creation requires POSIX")
def test_fifo_resource_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "image.png"
    os.mkfifo(fifo)
    path = tmp_path / "preview.svg"
    path.write_text(_image_svg("image.png"), encoding="utf-8")
    code = "from pathlib import Path; from smd_twin_lab.ui.safe_svg import prepare_svg; "
    code += "prepare_svg(Path(__import__('sys').argv[1]))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "regular files" in result.stderr


def test_opened_image_handle_cannot_escape_directory(application, tmp_path: Path, monkeypatch):
    local = tmp_path / "inside"
    local.mkdir()
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "outside.png"))
    preview = local / "preview.svg"
    preview.write_text(_image_svg("image.png"), encoding="utf-8")
    (local / "image.png").write_bytes(b"placeholder")
    original_open = os.open

    def swapped_open(path, *args, **kwargs):
        if path == local / "image.png":
            return original_open(tmp_path / "outside.png", *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(UnsafeSvg, match="outside"):
        prepare_svg(preview)


def test_allowed_directory_is_not_reresolved_during_image_open(application, tmp_path, monkeypatch):
    local = tmp_path / "inside"
    local.mkdir()
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.red)
    assert image.save(str(tmp_path / "outside.png"))
    preview = local / "preview.svg"
    preview.write_text(_image_svg("image.png"), encoding="utf-8")
    (local / "image.png").write_bytes(b"placeholder")
    original_open, original_resolve = os.open, Path.resolve
    base_resolves = 0

    def changed_resolve(path, *args, **kwargs):
        nonlocal base_resolves
        if path == local:
            base_resolves += 1
            return local if base_resolves == 1 else tmp_path
        return original_resolve(path, *args, **kwargs)

    def swapped_open(path, *args, **kwargs):
        if path == local / "image.png":
            return original_open(tmp_path / "outside.png", *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", changed_resolve)
    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(UnsafeSvg, match="outside"):
        prepare_svg(preview)
    assert base_resolves == 0


def test_nested_image_handle_cannot_swap_to_another_subdirectory(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    original = first / "nested.svg"
    replacement = second / "nested.svg"
    original.write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    replacement.write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    preview = tmp_path / "preview.svg"
    preview.write_text(_image_svg("a/nested.svg"), encoding="utf-8")
    original_open = os.open

    def swapped_open(path, *args, **kwargs):
        if path == original:
            return original_open(replacement, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(UnsafeSvg, match="does not match"):
        prepare_svg(preview)


def test_board_uses_validated_snapshot_even_if_file_changes(application, tmp_path, monkeypatch):
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    bundle = tmp_path / "project.json"
    bundle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "test",
                "geometry": {"top_preview_path": "preview.svg"},
            }
        ),
        encoding="utf-8",
    )
    project = load_bundle(bundle)
    original = board_canvas.prepare_svg

    def change_after_read(source):
        snapshot = original(source)
        source.write_bytes(EVIL)
        return snapshot

    monkeypatch.setattr(board_canvas, "prepare_svg", change_after_read)
    view = board_canvas.BoardView(ComponentSide.FRONT)
    view.set_project(project)
    items = [item for item in view.scene().items() if isinstance(item, QGraphicsSvgItem)]
    assert len(items) == 1
    renderer = items[0].renderer()
    assert renderer.isValid()
    assert renderer.parent() is items[0]
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    assert image.pixelColor(5, 5) == QtGui.QColor("red")
    view.close()


def test_board_skips_file_changed_after_bundle_load(application, tmp_path, caplog):
    path = tmp_path / "preview.svg"
    path.write_text(SVG_OPEN + RED + "</svg>", encoding="utf-8")
    bundle = tmp_path / "project.json"
    bundle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "test",
                "geometry": {"top_preview_path": "preview.svg"},
            }
        ),
        encoding="utf-8",
    )
    project = load_bundle(bundle)
    path.write_bytes(EVIL)
    view = board_canvas.BoardView(ComponentSide.FRONT)
    view.set_project(project)
    assert not any(isinstance(item, QGraphicsSvgItem) for item in view.scene().items())
    assert "Skipping SVG preview" in caplog.text
    view.close()
