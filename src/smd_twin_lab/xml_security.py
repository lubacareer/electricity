"""Bound resource use when parsing untrusted KiCad XML artifacts."""

import os
import stat
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError, TreeBuilder

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser

MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_NODES = 250_000
MAX_XML_DEPTH = 128
_CHUNK_SIZE = 64 * 1024


class XmlLimitExceeded(DefusedXmlException):
    """An XML artifact exceeds the supported resource limits."""


class UnsafeXmlSource(DefusedXmlException):
    """An XML artifact is not a regular file."""


class _BoundedTreeBuilder(TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self._nodes = 0
        self._depth = 0

    def start(self, tag: str, attrs: dict[str, str]) -> Element:
        self._nodes += 1
        self._depth += 1
        if self._nodes > MAX_XML_NODES:
            raise XmlLimitExceeded("XML node limit exceeded")
        if self._depth > MAX_XML_DEPTH:
            raise XmlLimitExceeded("XML nesting limit exceeded")
        return super().start(tag, attrs)

    def end(self, tag: str) -> Element:
        element = super().end(tag)
        self._depth -= 1
        return element


def parse_bounded_xml(path: Path) -> Element:
    """Parse a file incrementally with bounded bytes, elements and nesting."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise UnsafeXmlSource("XML source must be a regular file")
    parser = DefusedXMLParser(
        target=_BoundedTreeBuilder(),
        forbid_dtd=False,
        forbid_entities=True,
        forbid_external=True,
    )
    total = 0
    # Nonblocking open prevents a path swapped to a FIFO from hanging on POSIX.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise UnsafeXmlSource("XML source must be a regular file")
            while chunk := source.read(min(_CHUNK_SIZE, MAX_XML_BYTES - total + 1)):
                total += len(chunk)
                if total > MAX_XML_BYTES:
                    raise XmlLimitExceeded("XML byte limit exceeded")
                parser.feed(chunk)
        return parser.close()
    except LookupError as exc:
        raise ParseError("unknown or unsupported XML encoding") from exc
