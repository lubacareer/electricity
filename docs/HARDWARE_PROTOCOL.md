# USB/fixture protocol v1

The same envelope is used by the loopback target, future USB-UART board adapter,
and independent fixture controller. Transport is UTF-8 JSON Lines with one
message per line and a maximum encoded line length of 16,384 bytes.

```json
{"protocol_version":1,"request_id":"01J...","type":"identify","payload":{}}
```

Required fields:

- `protocol_version`: integer `1`.
- `request_id`: non-empty string, at most 128 characters; echoed by responses.
- `type`: non-empty string, at most 64 characters.
- `payload`: JSON object containing only finite JSON values.

Malformed UTF-8, invalid JSON, oversized or incomplete lines, non-finite numeric
values, and unknown protocol versions are rejected without changing device
state. A serial adapter must apply an explicit timeout, discard bytes through the
next newline after an oversized frame, and reconnect from a known state.

The first loopback command is `execute_scenario`; its response is `run_report`
with the same request ID. Future board commands will include `identify`,
`read_state`, `set_output`, and `reset`. Fixture-only stimulus and measurement
commands must stay on the fixture endpoint so test controls cannot leak into
production firmware.
