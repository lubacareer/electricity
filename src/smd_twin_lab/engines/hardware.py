"""Versioned JSON-Lines contract and an in-process hardware loopback."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict
from typing import Any

from ..contracts import CoSimulationSupervisor
from ..models import ImportedProject, RunReport, Scenario

PROTOCOL_VERSION = 1
DEFAULT_MAX_LINE_BYTES = 16_384


class ProtocolError(ValueError):
    """A bounded JSON-Lines message violated the hardware contract."""


def _reject_json_constant(value: str) -> None:
    raise ProtocolError(f"Non-finite JSON number {value!r} is not allowed")


def _validate_json_value(value: Any, *, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{path} contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"{path} object keys must be strings")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ProtocolError(f"{path} contains a non-JSON value")


class JsonLinesCodec:
    """Encode and decode one strict, bounded hardware protocol message."""

    def __init__(
        self,
        *,
        protocol_version: int = PROTOCOL_VERSION,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        if protocol_version <= 0:
            raise ValueError("protocol_version must be positive")
        if max_line_bytes < 128:
            raise ValueError("max_line_bytes must be at least 128")
        self.protocol_version = protocol_version
        self.max_line_bytes = max_line_bytes

    def make_message(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        message = {
            "protocol_version": self.protocol_version,
            "request_id": request_id or str(uuid.uuid4()),
            "type": message_type,
            "payload": payload,
        }
        self.validate(message)
        return message

    def validate(self, message: object) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise ProtocolError("Protocol message must be a JSON object")
        version = message.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProtocolError("protocol_version must be an integer")
        if version != self.protocol_version:
            raise ProtocolError(
                f"Unsupported protocol_version {version}; expected {self.protocol_version}"
            )
        request_id = message.get("request_id")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            or any(character in request_id for character in "\r\n")
        ):
            raise ProtocolError("request_id must be a non-empty, single-line string")
        message_type = message.get("type")
        if (
            not isinstance(message_type, str)
            or not message_type
            or len(message_type) > 64
            or any(character in message_type for character in "\r\n")
        ):
            raise ProtocolError("type must be a non-empty, single-line string")
        if "payload" not in message or not isinstance(message["payload"], dict):
            raise ProtocolError("payload must be a JSON object")
        _validate_json_value(message["payload"])
        return message

    def encode(self, message: dict[str, Any]) -> bytes:
        self.validate(message)
        try:
            encoded = (
                json.dumps(
                    message,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"Message cannot be encoded as JSON: {exc}") from exc
        if len(encoded) > self.max_line_bytes:
            raise ProtocolError(
                f"Protocol line is {len(encoded)} bytes; maximum is {self.max_line_bytes}"
            )
        return encoded

    def decode_line(self, line: bytes | str) -> dict[str, Any]:
        if isinstance(line, str):
            try:
                raw = line.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ProtocolError("Protocol line is not valid UTF-8") from exc
        else:
            raw = bytes(line)
        if len(raw) > self.max_line_bytes:
            raise ProtocolError(
                f"Protocol line is {len(raw)} bytes; maximum is {self.max_line_bytes}"
            )
        raw = raw.removesuffix(b"\n").removesuffix(b"\r")
        if not raw:
            raise ProtocolError("Protocol line is empty")
        if b"\n" in raw or b"\r" in raw:
            raise ProtocolError("decode_line accepts exactly one JSON line")
        try:
            text = raw.decode("utf-8")
            decoded = json.loads(text, parse_constant=_reject_json_constant)
        except UnicodeDecodeError as exc:
            raise ProtocolError("Protocol line is not valid UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Malformed JSON at column {exc.colno}") from exc
        return self.validate(decoded)


class JsonLinesDecoder:
    """Incrementally frame serial-port chunks into protocol messages."""

    def __init__(self, codec: JsonLinesCodec | None = None) -> None:
        self.codec = codec or JsonLinesCodec()
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[dict[str, Any], ...]:
        if not isinstance(chunk, bytes):
            raise TypeError("JSON-Lines chunks must be bytes")
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                if len(self._buffer) >= self.codec.max_line_bytes:
                    self._buffer.clear()
                    raise ProtocolError("Unterminated protocol line exceeds the byte limit")
                break
            framed = bytes(self._buffer[: newline_index + 1])
            del self._buffer[: newline_index + 1]
            messages.append(self.codec.decode_line(framed))
        return tuple(messages)

    def finish(self) -> None:
        if self._buffer:
            self._buffer.clear()
            raise ProtocolError("Serial stream ended with an incomplete JSON line")


class LoopbackHardwareTarget:
    """Exercise the exact hardware protocol before a serial target exists."""

    def __init__(
        self,
        supervisor: CoSimulationSupervisor,
        *,
        codec: JsonLinesCodec | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.codec = codec or JsonLinesCodec()

    @property
    def available(self) -> bool:
        return True

    def execute(self, project: ImportedProject, scenario: Scenario) -> RunReport:
        request_id = str(uuid.uuid4())
        request = self.codec.make_message(
            "execute_scenario",
            {"project_id": project.project_id, "scenario": asdict(scenario)},
            request_id=request_id,
        )
        decoded_request = self.codec.decode_line(self.codec.encode(request))
        if decoded_request["request_id"] != request_id:
            raise ProtocolError("Loopback request correlation failed")

        report = self.supervisor.run(project, scenario)
        response = self.codec.make_message(
            "run_report",
            report.to_dict(),
            request_id=request_id,
        )
        decoded_response = self.codec.decode_line(self.codec.encode(response))
        if decoded_response["request_id"] != request_id:
            raise ProtocolError("Loopback response correlation failed")
        return report
