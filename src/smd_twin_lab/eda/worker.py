"""Process-isolated EDA core protocol and Python fallback worker.

Protocol version 1 uses MessagePack preceded by a four-byte, big-endian body
length.  Every envelope names that codec and its protocol version, leaving an
explicit negotiation point for future codec versions without silently
interpreting incompatible bytes.

Only ``health`` and ``ping`` are implemented by this scaffold.  DRC and other
design operations fail with an explicit capability error; an unavailable
native implementation must never be mistaken for a successful empty report.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO, Never

import msgpack

CODEC_NAME = "length-prefixed-messagepack"
PROTOCOL_VERSION = 1
DEFAULT_MAX_FRAME_BYTES = 1_048_576
_HEADER = struct.Struct(">I")
_MAX_CONTAINER_DEPTH = 64
_NATIVE_EXECUTABLE_ENV = "SMD_TWIN_EDA_WORKER"


class MessageType(StrEnum):
    """Message kinds supported by protocol version 1."""

    REQUEST = "request"
    RESPONSE = "response"
    PROGRESS = "progress"
    ERROR = "error"
    CANCEL = "cancel"


class EdaWorkerError(RuntimeError):
    """Base class for worker lifecycle and remote-operation failures."""


class ProtocolError(EdaWorkerError, ValueError):
    """A frame or envelope violated the bounded worker protocol."""


class WorkerTimeoutError(EdaWorkerError, TimeoutError):
    """The worker did not finish before the caller's deadline."""


class WorkerCancelledError(EdaWorkerError):
    """The caller cancelled an in-flight worker request."""


class WorkerCrashedError(EdaWorkerError):
    """The worker process exited or its protocol stream failed."""


class RemoteWorkerError(EdaWorkerError):
    """A valid remote error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str,
        document_revision: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.document_revision = document_revision
        self.details = dict(details or {})


class CapabilityUnavailableError(RemoteWorkerError):
    """The protocol is working, but the selected backend lacks an operation."""


class StaleRevisionError(RemoteWorkerError):
    """The request referred to a document older than the worker's latest view."""


@dataclass(frozen=True, slots=True)
class WorkerResponse:
    """A correlated successful response and any preceding progress events."""

    request_id: str
    document_revision: int
    method: str
    payload: dict[str, Any]
    progress: tuple[dict[str, Any], ...] = ()


def _validate_messagepack_value(
    value: Any,
    *,
    path: str = "payload",
    depth: int = 0,
    ancestors: frozenset[int] = frozenset(),
) -> None:
    if depth > _MAX_CONTAINER_DEPTH:
        raise ProtocolError(f"{path} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**64 - 1:
            raise ProtocolError(f"{path} contains an integer outside MessagePack range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise ProtocolError(f"{path} contains a reference cycle")
        nested_ancestors = ancestors | {identity}
        for index, item in enumerate(value):
            _validate_messagepack_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                ancestors=nested_ancestors,
            )
        return
    if isinstance(value, dict):
        identity = id(value)
        if identity in ancestors:
            raise ProtocolError(f"{path} contains a reference cycle")
        nested_ancestors = ancestors | {identity}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"{path} object keys must be strings")
            _validate_messagepack_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                ancestors=nested_ancestors,
            )
        return
    raise ProtocolError(f"{path} contains an unsupported MessagePack value")


class LengthPrefixedMessagePackCodec:
    """Encode, validate, and incrementally decode bounded protocol frames."""

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if not 256 <= max_frame_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_frame_bytes must be between 256 bytes and 64 MiB")
        self.max_frame_bytes = max_frame_bytes

    def make_message(
        self,
        message_type: MessageType | str,
        *,
        request_id: str,
        document_revision: int,
        method: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "codec": CODEC_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "type": str(message_type),
            "request_id": request_id,
            "document_revision": document_revision,
            "method": method,
            "payload": dict(payload or {}),
        }
        return self.validate(message)

    def validate(self, message: object) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise ProtocolError("Worker message must be a MessagePack map")
        if message.get("codec") != CODEC_NAME:
            raise ProtocolError(f"Unsupported codec; expected {CODEC_NAME!r}")
        version = message.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProtocolError("protocol_version must be an integer")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"Unsupported protocol_version {version}; expected {PROTOCOL_VERSION}"
            )
        try:
            MessageType(message.get("type"))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("type is not a supported worker message kind") from exc
        request_id = message.get("request_id")
        if not _valid_single_line(request_id, max_length=128):
            raise ProtocolError("request_id must be a non-empty, single-line string")
        revision = message.get("document_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= 2**64 - 1
        ):
            raise ProtocolError("document_revision must be an unsigned 64-bit integer")
        method = message.get("method")
        if not _valid_single_line(method, max_length=128):
            raise ProtocolError("method must be a non-empty, single-line string")
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be a MessagePack map")
        _validate_messagepack_value(payload)
        return message

    def encode(self, message: Mapping[str, Any]) -> bytes:
        validated = self.validate(dict(message))
        try:
            body = msgpack.packb(
                validated,
                use_bin_type=True,
                strict_types=True,
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise ProtocolError(f"Message cannot be encoded as MessagePack: {exc}") from exc
        if len(body) > self.max_frame_bytes:
            raise ProtocolError(
                f"Frame body is {len(body)} bytes; maximum is {self.max_frame_bytes}"
            )
        return _HEADER.pack(len(body)) + body

    def decode(self, frame: bytes | bytearray | memoryview) -> dict[str, Any]:
        raw = bytes(frame)
        if len(raw) < _HEADER.size:
            raise ProtocolError("Frame is missing its four-byte length prefix")
        (body_length,) = _HEADER.unpack(raw[: _HEADER.size])
        self._validate_body_length(body_length)
        if len(raw) != _HEADER.size + body_length:
            raise ProtocolError(
                f"Frame length mismatch: prefix declares {body_length} bytes, "
                f"received {len(raw) - _HEADER.size}"
            )
        return self._decode_body(raw[_HEADER.size :])

    def read_from(self, stream: BinaryIO) -> dict[str, Any] | None:
        header = _read_exact(stream, _HEADER.size, allow_clean_eof=True)
        if header is None:
            return None
        (body_length,) = _HEADER.unpack(header)
        self._validate_body_length(body_length)
        body = _read_exact(stream, body_length, allow_clean_eof=False)
        assert body is not None
        return self._decode_body(body)

    def write_to(self, stream: BinaryIO, message: Mapping[str, Any]) -> None:
        stream.write(self.encode(message))
        stream.flush()

    def _decode_body(self, body: bytes) -> dict[str, Any]:
        try:
            decoded = msgpack.unpackb(
                body,
                raw=False,
                strict_map_key=True,
            )
        except (UnicodeDecodeError, ValueError, msgpack.exceptions.UnpackException) as exc:
            raise ProtocolError(f"Malformed MessagePack body: {exc}") from exc
        return self.validate(decoded)

    def _validate_body_length(self, body_length: int) -> None:
        if body_length == 0:
            raise ProtocolError("Zero-length frames are not allowed")
        if body_length > self.max_frame_bytes:
            raise ProtocolError(
                f"Frame body is {body_length} bytes; maximum is {self.max_frame_bytes}"
            )


class LengthPrefixedMessagePackDecoder:
    """Decode arbitrarily chunked process-pipe bytes without unbounded buffering."""

    def __init__(self, codec: LengthPrefixedMessagePackCodec | None = None) -> None:
        self.codec = codec or LengthPrefixedMessagePackCodec()
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[dict[str, Any], ...]:
        if not isinstance(chunk, bytes):
            raise TypeError("Worker stream chunks must be bytes")
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []
        while len(self._buffer) >= _HEADER.size:
            (body_length,) = _HEADER.unpack(self._buffer[: _HEADER.size])
            try:
                self.codec._validate_body_length(body_length)
            except ProtocolError:
                self._buffer.clear()
                raise
            frame_length = _HEADER.size + body_length
            if len(self._buffer) < frame_length:
                break
            frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            messages.append(self.codec.decode(frame))
        return tuple(messages)

    def finish(self) -> None:
        if self._buffer:
            self._buffer.clear()
            raise ProtocolError("Worker stream ended with an incomplete frame")


def _read_exact(stream: BinaryIO, size: int, *, allow_clean_eof: bool) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            if not data and allow_clean_eof:
                return None
            raise ProtocolError("Worker stream ended with an incomplete frame")
        data.extend(chunk)
    return bytes(data)


def _valid_single_line(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_length
        and "\r" not in value
        and "\n" not in value
        and "\x00" not in value
    )


class PythonWorkerBackend:
    """Minimal deterministic backend used when the Rust binary is unavailable."""

    def __init__(self, *, backend_name: str = "python-fallback") -> None:
        self.backend_name = backend_name
        self.latest_revision = -1
        self._cancelled_request_ids: set[str] = set()
        self.codec = LengthPrefixedMessagePackCodec()

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any]:
        request = self.codec.validate(dict(message))
        message_type = MessageType(request["type"])
        if message_type is MessageType.CANCEL:
            self._cancelled_request_ids.add(request["request_id"])
            return self._response(request, {"cancelled": True})
        if message_type is not MessageType.REQUEST:
            return self._error(
                request,
                "unexpected_message_type",
                "The worker accepts only request and cancel messages from clients.",
            )

        revision = request["document_revision"]
        if revision < self.latest_revision:
            return self._error(
                request,
                "stale_revision",
                "The request document revision is older than the worker state.",
                {"latest_revision": self.latest_revision},
            )
        self.latest_revision = revision
        if request["request_id"] in self._cancelled_request_ids:
            return self._error(request, "cancelled", "The request was cancelled.")

        method = request["method"]
        if method == "health":
            return self._response(
                request,
                {
                    "status": "ready",
                    "backend": self.backend_name,
                    "protocol": {
                        "codec": CODEC_NAME,
                        "version": PROTOCOL_VERSION,
                        "supported_codecs": [CODEC_NAME],
                    },
                    "capabilities": {
                        "health": {"available": True},
                        "ping": {"available": True},
                        "check_drc": {
                            "available": False,
                            "reason": "DRC is not implemented by this worker scaffold.",
                        },
                    },
                },
            )
        if method == "ping":
            return self._response(request, {"echo": request["payload"]})
        if method == "check_drc":
            return self._error(
                request,
                "capability_unavailable",
                "DRC is not implemented by this worker backend.",
                {"capability": "check_drc", "backend": self.backend_name},
            )
        return self._error(
            request,
            "unsupported_method",
            f"Worker method {method!r} is not supported.",
            {"method": method},
        )

    def _response(self, request: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.codec.make_message(
            MessageType.RESPONSE,
            request_id=request["request_id"],
            document_revision=request["document_revision"],
            method=request["method"],
            payload=payload,
        )

    def _error(
        self,
        request: Mapping[str, Any],
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.codec.make_message(
            MessageType.ERROR,
            request_id=request["request_id"],
            document_revision=request["document_revision"],
            method=request["method"],
            payload={"code": code, "message": message, "details": dict(details or {})},
        )


def serve(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    codec: LengthPrefixedMessagePackCodec | None = None,
    backend: PythonWorkerBackend | None = None,
) -> None:
    """Serve requests until clean EOF; malformed framing terminates the worker."""

    selected_codec = codec or LengthPrefixedMessagePackCodec()
    selected_backend = backend or PythonWorkerBackend()
    selected_backend.codec = selected_codec
    while True:
        request = selected_codec.read_from(input_stream)
        if request is None:
            return
        selected_codec.write_to(output_stream, selected_backend.handle(request))


@dataclass(frozen=True, slots=True)
class _ReaderFailure:
    error: BaseException


@dataclass(frozen=True, slots=True)
class _ReaderClosed:
    returncode: int | None


_QueueItem = dict[str, Any] | _ReaderFailure | _ReaderClosed


def discover_native_worker() -> tuple[str, ...] | None:
    """Locate an explicitly configured, installed, or locally built Rust worker."""

    configured = os.environ.get(_NATIVE_EXECUTABLE_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return (str(candidate.resolve()),)

    executable_name = "smd-twin-eda-core.exe" if os.name == "nt" else "smd-twin-eda-core"
    installed = shutil.which(executable_name)
    if installed:
        return (installed,)

    try:
        repository_root = Path(__file__).resolve().parents[3]
    except IndexError:
        return None
    for profile in ("release", "debug"):
        candidate = repository_root / "target" / profile / executable_name
        if candidate.is_file():
            return (str(candidate),)
    return None


class EdaWorkerClient:
    """Synchronous, restartable client for one isolated EDA worker process.

    Calls are serialized in protocol version 1.  A timeout, cancellation, or
    malformed stream terminates that worker, preventing a late response from
    being confused with a subsequent request.  A later request starts a new
    process automatically.  Failed requests are not retried because future EDA
    methods may mutate worker-owned state.
    """

    def __init__(
        self,
        command: Sequence[str | Path] | None = None,
        *,
        timeout_s: float = 5.0,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        prefer_native: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if command is not None and (not command or not str(command[0])):
            raise ValueError("command must contain an executable")
        native = discover_native_worker() if command is None and prefer_native else None
        self.command = tuple(str(part) for part in (command or native or self.python_command()))
        self.backend_kind = "native" if native is not None and command is None else "python"
        if command is not None:
            self.backend_kind = "custom"
        self.timeout_s = timeout_s
        self.codec = LengthPrefixedMessagePackCodec(max_frame_bytes=max_frame_bytes)
        self.env = dict(env or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[_QueueItem] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._request_lock = threading.Lock()
        self._latest_revision = 0
        self._generation = 0

    @staticmethod
    def python_command() -> tuple[str, ...]:
        return (sys.executable, "-u", "-m", "smd_twin_lab.eda.worker", "--serve")

    @property
    def generation(self) -> int:
        """Number of worker processes started by this client."""

        return self._generation

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def health(self, *, document_revision: int | None = None) -> WorkerResponse:
        return self.request(
            "health",
            {},
            document_revision=(
                self._latest_revision if document_revision is None else document_revision
            ),
        )

    def check_drc(
        self,
        document_revision: int,
        design: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        cancel_event: Event | None = None,
    ) -> WorkerResponse:
        return self.request(
            "check_drc",
            {"design": dict(design)},
            document_revision=document_revision,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )

    def request(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        document_revision: int,
        timeout_s: float | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> WorkerResponse:
        selected_timeout = self.timeout_s if timeout_s is None else timeout_s
        if selected_timeout <= 0:
            raise ValueError("timeout_s must be positive")
        if cancel_event is not None and cancel_event.is_set():
            raise WorkerCancelledError("EDA worker request was cancelled before it started")

        with self._request_lock:
            process = self._ensure_started()
            request_id = str(uuid.uuid4())
            request = self.codec.make_message(
                MessageType.REQUEST,
                request_id=request_id,
                document_revision=document_revision,
                method=method,
                payload=payload,
            )
            try:
                assert process.stdin is not None
                self.codec.write_to(process.stdin, request)
            except (BrokenPipeError, OSError, ProtocolError) as exc:
                detail = self._crash_detail(process)
                self._stop_process()
                raise WorkerCrashedError(f"EDA worker request could not be sent{detail}") from exc

            deadline = time.monotonic() + selected_timeout
            progress: list[dict[str, Any]] = []
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._send_cancel(process, request)
                    self._stop_process()
                    raise WorkerCancelledError("EDA worker request was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._send_cancel(process, request)
                    self._stop_process()
                    raise WorkerTimeoutError(
                        f"EDA worker method {method!r} exceeded {selected_timeout:g} seconds"
                    )
                try:
                    item = self._messages.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    if process.poll() is not None:
                        detail = self._crash_detail(process)
                        self._stop_process()
                        raise WorkerCrashedError(
                            f"EDA worker exited before responding{detail}"
                        ) from None
                    continue
                if isinstance(item, _ReaderFailure):
                    detail = self._crash_detail(process)
                    self._stop_process()
                    raise WorkerCrashedError(
                        f"EDA worker protocol stream failed{detail}"
                    ) from item.error
                if isinstance(item, _ReaderClosed):
                    detail = self._crash_detail(process, item.returncode)
                    self._stop_process()
                    raise WorkerCrashedError(f"EDA worker exited before responding{detail}")
                if item["request_id"] != request_id:
                    self._stop_process()
                    raise ProtocolError("Worker returned a response for an unexpected request_id")
                if item["document_revision"] != document_revision or item["method"] != method:
                    self._stop_process()
                    raise ProtocolError(
                        "Worker response correlation fields do not match the request"
                    )

                message_type = MessageType(item["type"])
                if message_type is MessageType.PROGRESS:
                    event = dict(item["payload"])
                    progress.append(event)
                    if progress_callback is not None:
                        progress_callback(event)
                    continue
                if message_type is MessageType.RESPONSE:
                    self._latest_revision = max(self._latest_revision, document_revision)
                    return WorkerResponse(
                        request_id=request_id,
                        document_revision=document_revision,
                        method=method,
                        payload=dict(item["payload"]),
                        progress=tuple(progress),
                    )
                if message_type is MessageType.ERROR:
                    self._latest_revision = max(self._latest_revision, document_revision)
                    self._raise_remote_error(item)
                self._stop_process()
                raise ProtocolError(f"Unexpected worker response type {message_type.value!r}")

    def restart(self) -> None:
        with self._request_lock:
            self._stop_process()
            self._ensure_started()

    def close(self) -> None:
        with self._request_lock:
            self._stop_process()

    def __enter__(self) -> EdaWorkerClient:
        self._ensure_started()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._stop_process()
        self._messages = queue.Queue()
        self._stderr_tail = deque(maxlen=40)
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            popen_kwargs["creationflags"] = creation_flags
        else:
            popen_kwargs["start_new_session"] = True
        child_env = os.environ.copy()
        child_env.update(self.env)
        process = subprocess.Popen(  # noqa: S603 - command is always an argv array
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=child_env,
            **popen_kwargs,
        )
        self._process = process
        self._generation += 1
        message_queue = self._messages
        stderr_tail = self._stderr_tail
        self._reader_thread = threading.Thread(
            target=self._read_messages,
            args=(process, message_queue),
            name="eda-worker-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process, stderr_tail),
            name="eda-worker-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        return process

    def _read_messages(
        self,
        process: subprocess.Popen[bytes],
        message_queue: queue.Queue[_QueueItem],
    ) -> None:
        try:
            assert process.stdout is not None
            while True:
                message = self.codec.read_from(process.stdout)
                if message is None:
                    message_queue.put(_ReaderClosed(process.poll()))
                    return
                message_queue.put(message)
        except BaseException as exc:  # noqa: BLE001 - transported to the request thread
            message_queue.put(_ReaderFailure(exc))

    @staticmethod
    def _read_stderr(process: subprocess.Popen[bytes], stderr_tail: deque[str]) -> None:
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    def _send_cancel(self, process: subprocess.Popen[bytes], request: Mapping[str, Any]) -> None:
        if process.poll() is not None or process.stdin is None:
            return
        cancel = self.codec.make_message(
            MessageType.CANCEL,
            request_id=request["request_id"],
            document_revision=request["document_revision"],
            method=request["method"],
            payload={},
        )
        with contextlib.suppress(BrokenPipeError, OSError):
            self.codec.write_to(process.stdin, cancel)

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._reader_thread = None
        self._stderr_thread = None

    def _crash_detail(self, process: subprocess.Popen[bytes], returncode: int | None = None) -> str:
        selected_returncode = process.poll() if returncode is None else returncode
        detail = "" if selected_returncode is None else f" (exit code {selected_returncode})"
        stderr = " | ".join(line for line in self._stderr_tail if line)
        if stderr:
            detail += f": {stderr}"
        return detail

    @staticmethod
    def _raise_remote_error(message: Mapping[str, Any]) -> Never:
        payload = message["payload"]
        code = payload.get("code")
        text = payload.get("message")
        details = payload.get("details", {})
        if not isinstance(code, str) or not isinstance(text, str) or not isinstance(details, dict):
            raise ProtocolError("Worker error payload is malformed")
        error_type: type[RemoteWorkerError] = RemoteWorkerError
        if code == "capability_unavailable":
            error_type = CapabilityUnavailableError
        elif code == "stale_revision":
            error_type = StaleRevisionError
        elif code == "cancelled":
            raise WorkerCancelledError(text)
        raise error_type(
            code,
            text,
            request_id=message["request_id"],
            document_revision=message["document_revision"],
            details=details,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMD Twin Lab EDA core worker")
    parser.add_argument("--serve", action="store_true", help="serve framed requests on stdio")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.serve:
        print("Use --serve to run the EDA worker protocol.", file=sys.stderr)
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ProtocolError) as exc:
        print(f"EDA worker protocol failure: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
