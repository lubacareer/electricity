from __future__ import annotations

import io
import math
import queue
import struct
import sys
from pathlib import Path
from threading import Event

import msgpack
import pytest

from smd_twin_lab.eda.worker import (
    CODEC_NAME,
    PROTOCOL_VERSION,
    CapabilityUnavailableError,
    EdaWorkerClient,
    LengthPrefixedMessagePackCodec,
    LengthPrefixedMessagePackDecoder,
    MessageType,
    ProtocolError,
    StaleRevisionError,
    WorkerCancelledError,
    WorkerTimeoutError,
)


def message(
    codec: LengthPrefixedMessagePackCodec,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return codec.make_message(
        MessageType.REQUEST,
        request_id="request-1",
        document_revision=7,
        method="ping",
        payload=payload,
    )


def test_length_prefixed_codec_is_incremental_and_explicit() -> None:
    codec = LengthPrefixedMessagePackCodec()
    request = message(codec, payload={"label": "АЦП", "value": 1.65})
    encoded = codec.encode(request)
    decoder = LengthPrefixedMessagePackDecoder(codec)

    (declared_length,) = struct.unpack(">I", encoded[:4])
    assert declared_length == len(encoded) - 4
    assert decoder.feed(encoded[:3]) == ()
    assert decoder.feed(encoded[3:9]) == ()
    assert decoder.feed(encoded[9:]) == (request,)
    decoder.finish()
    assert request["codec"] == CODEC_NAME
    assert request["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.parametrize(
    "broken",
    [
        lambda codec: codec.encode(message(codec))[:-1],
        lambda codec: b"\x00\x00\x00\x00",
        lambda codec: struct.pack(">I", codec.max_frame_bytes + 1),
    ],
)
def test_codec_rejects_incomplete_zero_and_oversized_frames(broken: object) -> None:
    codec = LengthPrefixedMessagePackCodec()
    with pytest.raises(ProtocolError):
        codec.decode(broken(codec))  # type: ignore[operator]


def test_codec_rejects_non_finite_and_cyclic_payloads() -> None:
    codec = LengthPrefixedMessagePackCodec()
    with pytest.raises(ProtocolError, match="non-finite"):
        message(codec, payload={"value": math.nan})

    body = msgpack.packb(
        {
            "codec": CODEC_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "type": "request",
            "request_id": "request-1",
            "document_revision": 0,
            "method": "ping",
            "payload": {"value": math.inf},
        },
        use_bin_type=True,
    )
    with pytest.raises(ProtocolError, match="non-finite"):
        codec.decode(struct.pack(">I", len(body)) + body)

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ProtocolError, match="cycle"):
        message(codec, payload=cyclic)


def test_python_fallback_health_ping_and_explicit_drc_capability() -> None:
    with EdaWorkerClient(prefer_native=False, timeout_s=3.0) as client:
        health = client.health(document_revision=0)
        assert health.payload["status"] == "ready"
        assert health.payload["backend"] == "python-fallback"
        assert health.payload["capabilities"]["check_drc"]["available"] is False

        ping = client.request("ping", {"value": 42}, document_revision=1)
        assert ping.payload == {"echo": {"value": 42}}

        with pytest.raises(CapabilityUnavailableError) as error:
            client.check_drc(2, {"board": {}})
        assert error.value.code == "capability_unavailable"
        assert error.value.details["capability"] == "check_drc"


def test_worker_rejects_stale_document_revision() -> None:
    with EdaWorkerClient(prefer_native=False, timeout_s=3.0) as client:
        client.health(document_revision=5)
        with pytest.raises(StaleRevisionError) as error:
            client.request("ping", {}, document_revision=4)
        assert error.value.details == {"latest_revision": 5}


def test_timeout_and_preexisting_cancellation_are_bounded() -> None:
    sleeper = (sys.executable, "-c", "import time; time.sleep(10)")
    client = EdaWorkerClient(command=sleeper, timeout_s=0.05)
    try:
        with pytest.raises(WorkerTimeoutError):
            client.health(document_revision=0)
        assert client.worker_pid is None
    finally:
        client.close()

    cancelled = Event()
    cancelled.set()
    with EdaWorkerClient(prefer_native=False) as fallback, pytest.raises(WorkerCancelledError):
        fallback.request("ping", {}, document_revision=0, cancel_event=cancelled)


def test_response_queued_after_short_timeout_wins_over_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.poll_calls = 0

        def poll(self) -> int:
            self.poll_calls += 1
            return 0

    client = EdaWorkerClient(prefer_native=False, timeout_s=1.0)
    process = ExitedProcess()
    messages: queue.Queue[dict[str, object]] = queue.Queue()
    response = client.codec.make_message(
        MessageType.RESPONSE,
        request_id="request-1",
        document_revision=7,
        method="ping",
        payload={"status": "ready"},
    )
    original_get = messages.get
    first_get = True

    def get_after_short_timeout(*, timeout: float) -> dict[str, object]:
        nonlocal first_get
        if first_get:
            first_get = False
            messages.put(response)
            raise queue.Empty
        return original_get(timeout=timeout)

    monkeypatch.setattr("smd_twin_lab.eda.worker.uuid.uuid4", lambda: "request-1")
    monkeypatch.setattr(messages, "get", get_after_short_timeout)
    monkeypatch.setattr(client, "_ensure_started", lambda: process)
    client._messages = messages

    result = client.request("ping", {}, document_revision=7)

    assert result.payload == {"status": "ready"}
    assert process.poll_calls == 0


def test_worker_restarts_after_process_exit() -> None:
    with EdaWorkerClient(prefer_native=False, timeout_s=3.0) as client:
        first = client.health(document_revision=0)
        assert first.payload["status"] == "ready"
        process = client._process
        assert process is not None
        first_pid = process.pid
        process.kill()
        process.wait(timeout=3.0)

        second = client.health(document_revision=0)
        assert second.payload["status"] == "ready"
        assert client.generation == 2
        assert client.worker_pid != first_pid


def test_native_worker_when_built_uses_the_same_protocol() -> None:
    executable = Path("target/debug/smd-twin-eda-core")
    if sys.platform == "win32":
        executable = executable.with_suffix(".exe")
    if not executable.is_file():
        pytest.skip("native Rust worker is not built")

    with EdaWorkerClient(command=(executable,), timeout_s=3.0) as client:
        health = client.health(document_revision=0)
        assert health.payload["backend"] == "rust"
        with pytest.raises(CapabilityUnavailableError):
            client.check_drc(1, {})
