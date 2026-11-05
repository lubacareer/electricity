"""Prepare a self-contained, bounded SVG snapshot for Qt rendering."""

from __future__ import annotations

import base64
import binascii
import io
import math
import os
import re
import stat
import sys
import xml.etree.ElementTree as std_et
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote_to_bytes, urlsplit

from defusedxml import ElementTree as safe_et
from defusedxml.common import DefusedXmlException
from PySide6 import QtCore, QtGui

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 4
MAX_XML_DEPTH = 128
MAX_XML_NODES = 100_000
MAX_TOTAL_PIXELS = 16_000_000
MAX_SVG_DIMENSION = 16_384
MAX_REFERENCE_EXPANSION = 100_000
MAX_VIEWBOX_COORDINATE = 1_000_000_000
_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "symbol",
        "use",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "desc",
        "image",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "mask",
        "pattern",
        "style",
    }
)
_URL_START = re.compile(r"url\s*\(", re.IGNORECASE)
_URL_FRAGMENT = re.compile(r"\s*(['\"]?)(#[A-Za-z_][A-Za-z0-9_.:-]*)\1\s*\)")


class UnsafeSvg(ValueError):
    """A preview cannot be rendered under the supported resource policy."""


@dataclass
class _Budget:
    remaining: int = MAX_TOTAL_BYTES
    pixels: int = MAX_TOTAL_PIXELS

    def consume(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise UnsafeSvg("SVG resource size limit exceeded")

    def consume_pixels(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or width > MAX_TOTAL_PIXELS // height:
            raise UnsafeSvg("invalid or oversized image dimensions")
        self.pixels -= width * height
        if self.pixels < 0:
            raise UnsafeSvg("SVG image pixel limit exceeded")


def _raster_pixels(raw: bytes, mime: str, budget: _Budget) -> None:
    buffer = QtCore.QBuffer()
    buffer.setData(QtCore.QByteArray(raw))
    buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
    formats = {"image/png": b"png", "image/jpeg": b"jpeg", "image/gif": b"gif"}
    reader = QtGui.QImageReader(buffer, QtCore.QByteArray(formats[mime]))
    size = reader.size()
    budget.consume_pixels(size.width(), size.height())


def _svg_pixels(root: std_et.Element, budget: _Budget) -> None:
    width, height = root.get("width"), root.get("height")
    viewbox = root.get("viewBox")
    box = None
    if viewbox:
        box = re.split(r"[,\s]+", viewbox.strip())
        try:
            coordinates = [float(value) for value in box]
        except ValueError as exc:
            raise UnsafeSvg("invalid SVG viewBox") from exc
        if (
            len(coordinates) != 4
            or any(
                not math.isfinite(value) or abs(value) > MAX_VIEWBOX_COORDINATE
                for value in coordinates
            )
            or coordinates[2] <= 0
            or coordinates[3] <= 0
        ):
            raise UnsafeSvg("invalid or oversized SVG viewBox")
    if width is not None and height is not None:
        values = (width, height)
    elif width is None and height is None and box:
        values = (box[2], box[3])
    else:
        raise UnsafeSvg("SVG requires bounded dimensions")
    sizes = []
    for value in values:
        match = re.fullmatch(
            r"((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(px|mm|cm|in|pt|pc)?", value
        )
        if not match:
            raise UnsafeSvg("SVG requires bounded absolute dimensions")
        scale = {
            None: 1,
            "px": 1,
            "mm": 96 / 25.4,
            "cm": 96 / 2.54,
            "in": 96,
            "pt": 96 / 72,
            "pc": 16,
        }[match.group(2)]
        number = float(match.group(1)) * scale
        if not math.isfinite(number) or number > MAX_SVG_DIMENSION:
            raise UnsafeSvg("invalid or oversized image dimensions")
        sizes.append(math.ceil(number))
    budget.consume_pixels(*sizes)


def _opened_file_path(source: io.BufferedReader) -> Path:
    """Resolve the file actually opened, not a path that can be swapped."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        get_path.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(source.fileno())
        output = ctypes.create_unicode_buffer(32768)
        length = get_path(handle, output, len(output), 0)
        if not length or length >= len(output):
            raise UnsafeSvg("cannot verify opened image resource")
        result = output.value
        if result.casefold().startswith("\\\\?\\unc\\"):
            raise UnsafeSvg("remote image resources are unsupported")
        if result.startswith("\\\\?\\"):
            result = result[4:]
        return Path(result)
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink(f"/proc/self/fd/{source.fileno()}"))
        except OSError as exc:
            raise UnsafeSvg("cannot verify opened image resource") from exc
    raise UnsafeSvg("local image resources are unsupported on this platform")


def _read_bounded(path: Path, budget: _Budget, *, within: Path | None = None) -> bytes:
    try:
        if os.name == "nt" and os.path.isreserved(str(path)):
            raise UnsafeSvg("reserved device paths are unsupported")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeSvg("SVG resources must be regular files")
            source = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with source:
            if within is not None:
                try:
                    opened_path = _opened_file_path(source)
                    opened_path.relative_to(within)
                except ValueError as exc:
                    raise UnsafeSvg("opened image is outside its SVG directory") from exc
                if opened_path != path:
                    raise UnsafeSvg("opened image does not match the selected resource")
            data = source.read(budget.remaining + 1)
    except OSError as exc:
        raise UnsafeSvg(f"cannot read SVG resource: {exc}") from exc
    budget.consume(len(data))
    return data


def _check_css(value: str) -> list[str]:
    # No escaped CSS or at-rules: both make resource references ambiguous.
    if "\\" in value or "@" in value or "/*" in value:
        raise UnsafeSvg("CSS escapes, comments and at-rules are unsupported")
    references = []
    position = 0
    while start := _URL_START.search(value, position):
        # Fail at the first malformed reference rather than repeatedly scanning the
        # remainder of an attacker-controlled value for a closing parenthesis.
        fragment = _URL_FRAGMENT.match(value, start.end())
        if fragment is None:
            raise UnsafeSvg("external or malformed CSS resources are unsupported")
        references.append(fragment.group(2)[1:])
        position = fragment.end()
    return references


def _resource(value: str, base: Path, budget: _Budget, depth: int) -> str:
    if value[:5].casefold() == "data:":
        header, sep, encoded = value.partition(",")
        if not sep:
            raise UnsafeSvg("invalid image data URI")
        raw = unquote_to_bytes(encoded)
        try:
            if header.casefold().endswith(";base64"):
                raw = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UnsafeSvg("invalid image data URI") from exc
        budget.consume(len(raw))
        resource_base = base
    else:
        try:
            uri = urlsplit(value)
        except ValueError as exc:
            raise UnsafeSvg("invalid image resource URI") from exc
        if uri.scheme or uri.netloc or uri.query or uri.fragment or "\\" in value:
            raise UnsafeSvg("only relative local image resources are supported")
        try:
            relative = unquote_to_bytes(uri.path).decode("utf-8", errors="strict")
            if (
                "\\" in relative
                or ".." in relative.split("/")
                or Path(relative).is_absolute()
                or PureWindowsPath(relative).anchor
            ):
                raise ValueError("absolute or anchored image path")
            path = (base / relative).resolve()
            path.relative_to(base)
        except (ValueError, UnicodeError, OSError, RuntimeError) as exc:
            raise UnsafeSvg("image resource must stay within its SVG directory") from exc
        raw = _read_bounded(path, budget, within=base)
        resource_base = path.parent

    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    else:
        mime = "image/svg+xml"
        raw = _prepare(raw, resource_base, budget, depth + 1)
    if mime != "image/svg+xml":
        _raster_pixels(raw, mime, budget)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _check_reference_graph(elements, ids, references) -> None:
    """Bound both recursive and exponentially expanding in-document references."""
    edges = {element: list(element) for element in elements}
    for source, name in references:
        target = ids.get(name)
        if target is not None:
            edges[source].append(target)
    states = {}
    weights = {}
    for start in elements:
        if states.get(start):
            continue
        states[start] = 1
        stack = [(start, 0)]
        while stack:
            element, index = stack[-1]
            children = edges[element]
            if index == len(children):
                weight = 1 + sum(weights[child] for child in children)
                if weight > MAX_REFERENCE_EXPANSION:
                    raise UnsafeSvg("SVG reference expansion limit exceeded")
                weights[element] = weight
                states[element] = 2
                stack.pop()
                continue
            child = children[index]
            stack[-1] = (element, index + 1)
            if states.get(child) == 1:
                raise UnsafeSvg("cyclic SVG references are unsupported")
            if not states.get(child):
                states[child] = 1
                stack.append((child, 0))


def _prepare(raw: bytes, base: Path, budget: _Budget, depth: int) -> bytes:
    if depth > MAX_DEPTH:
        raise UnsafeSvg("nested SVG depth limit exceeded")
    try:
        parser = safe_et.iterparse(io.BytesIO(raw), events=("start", "end"))
        nesting = 0
        count = 0
        for event, _ in parser:
            if event == "start":
                count += 1
                nesting += 1
                if count > MAX_XML_NODES or nesting > MAX_XML_DEPTH + 1:
                    raise UnsafeSvg("SVG XML complexity limit exceeded")
            else:
                nesting -= 1
        root = parser.root
    except (std_et.ParseError, DefusedXmlException, LookupError, ValueError) as exc:
        raise UnsafeSvg(f"invalid SVG XML: {exc}") from exc
    if root.tag not in ("svg", f"{{{SVG_NS}}}svg"):
        raise UnsafeSvg("preview root element is not SVG")
    _svg_pixels(root, budget)

    pending = [(root, 0)]
    elements = []
    references = []
    ids = {}
    count = 0
    while pending:
        element, xml_depth = pending.pop()
        count += 1
        if count > MAX_XML_NODES or xml_depth > MAX_XML_DEPTH:
            raise UnsafeSvg("SVG XML complexity limit exceeded")
        pending.extend((child, xml_depth + 1) for child in element)
        if not isinstance(element.tag, str):
            raise UnsafeSvg("unsupported SVG node")
        if element.tag.startswith("{"):
            namespace, _, local = element.tag[1:].partition("}")
        else:
            namespace, local = "", element.tag
        if namespace not in ("", SVG_NS) or local not in _ELEMENTS:
            raise UnsafeSvg(f"unsupported SVG element: {local}")
        elements.append(element)
        element_id = element.get("id")
        if element_id:
            if element_id in ids:
                raise UnsafeSvg("duplicate SVG identifiers are unsupported")
            ids[element_id] = element
        if not namespace:
            element.tag = f"{{{SVG_NS}}}{local}"
        if XML_BASE in element.attrib:
            raise UnsafeSvg("xml:base is unsupported")
        hrefs = [key for key in element.attrib if key == "href" or key.endswith("}href")]
        if len(hrefs) > 1 or any(key not in ("href", XLINK_HREF) for key in hrefs):
            raise UnsafeSvg("ambiguous SVG resource reference")
        if hrefs:
            key = hrefs[0]
            value = element.attrib[key]
            if local == "image":
                element.attrib[key] = _resource(value, base, budget, depth)
            elif not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.:-]*", value):
                raise UnsafeSvg("only internal SVG references are supported")
            else:
                references.append((element, value[1:]))
        for key, value in element.attrib.items():
            if key not in ("href", XLINK_HREF):
                references.extend((element, name) for name in _check_css(value))
        if local == "style":
            if len(element):
                raise UnsafeSvg("nested style elements are unsupported")
            if element.text and _check_css(element.text):
                raise UnsafeSvg("SVG stylesheet fragment references are unsupported")

    _check_reference_graph(elements, ids, references)

    # Qt's embedded SVG decoder requires the default SVG namespace.
    std_et.register_namespace("", SVG_NS)
    std_et.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    result = std_et.tostring(root, encoding="utf-8")
    budget.consume(len(result))
    return result


def prepare_svg(path: Path) -> bytes:
    """Read and validate each resource once and return the exact bytes Qt receives."""
    budget = _Budget()
    try:
        path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise UnsafeSvg(f"cannot resolve SVG resource: {exc}") from exc
    return _prepare(_read_bounded(path, budget), path.parent, budget, 0)
