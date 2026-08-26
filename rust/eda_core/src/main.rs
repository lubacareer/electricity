//! Minimal native EDA worker.
//!
//! Protocol v1 uses bounded, length-prefixed MessagePack.  Only health and ping
//! are implemented here; DRC returns an explicit capability error until the
//! real geometry engine exists.

use serde_json::{Map, Value, json};
use std::io::{self, ErrorKind, Read, Write};

const CODEC_NAME: &str = "length-prefixed-messagepack";
const PROTOCOL_VERSION: u64 = 1;
const MAX_FRAME_BYTES: usize = 1_048_576;

#[derive(Debug)]
struct Request<'a> {
    message_type: &'a str,
    request_id: &'a str,
    document_revision: u64,
    method: &'a str,
    payload: &'a Map<String, Value>,
}

fn parse_request(value: &Value) -> Result<Request<'_>, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "worker message must be a MessagePack map".to_owned())?;
    if object.get("codec").and_then(Value::as_str) != Some(CODEC_NAME) {
        return Err(format!("unsupported codec; expected {CODEC_NAME:?}"));
    }
    if object.get("protocol_version").and_then(Value::as_u64) != Some(PROTOCOL_VERSION) {
        return Err(format!(
            "unsupported protocol_version; expected {PROTOCOL_VERSION}"
        ));
    }
    let message_type = required_single_line(object, "type", 64)?;
    if !matches!(message_type, "request" | "cancel") {
        return Err("worker accepts only request and cancel messages".to_owned());
    }
    let request_id = required_single_line(object, "request_id", 128)?;
    let document_revision = object
        .get("document_revision")
        .and_then(Value::as_u64)
        .ok_or_else(|| "document_revision must be a non-negative integer".to_owned())?;
    let method = required_single_line(object, "method", 128)?;
    let payload = object
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| "payload must be a MessagePack map".to_owned())?;
    Ok(Request {
        message_type,
        request_id,
        document_revision,
        method,
        payload,
    })
}

fn required_single_line<'a>(
    object: &'a Map<String, Value>,
    key: &str,
    max_length: usize,
) -> Result<&'a str, String> {
    let value = object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{key} must be a string"))?;
    if value.is_empty() || value.len() > max_length || value.contains(['\r', '\n', '\0']) {
        return Err(format!(
            "{key} must be a non-empty, bounded, single-line string"
        ));
    }
    Ok(value)
}

fn envelope(request: &Request<'_>, message_type: &str, payload: Value) -> Value {
    json!({
        "codec": CODEC_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request.request_id,
        "document_revision": request.document_revision,
        "method": request.method,
        "payload": payload,
    })
}

fn error_response(request: &Request<'_>, code: &str, message: &str, details: Value) -> Value {
    envelope(
        request,
        "error",
        json!({"code": code, "message": message, "details": details}),
    )
}

fn handle(value: &Value, latest_revision: &mut Option<u64>) -> Result<Value, String> {
    let request = parse_request(value)?;
    if request.message_type == "cancel" {
        return Ok(envelope(&request, "response", json!({"cancelled": true})));
    }
    if let Some(latest) = latest_revision
        && request.document_revision < *latest
    {
        return Ok(error_response(
            &request,
            "stale_revision",
            "The request document revision is older than the worker state.",
            json!({"latest_revision": latest}),
        ));
    }
    *latest_revision = Some(request.document_revision);

    match request.method {
        "health" => Ok(envelope(
            &request,
            "response",
            json!({
                "status": "ready",
                "backend": "rust",
                "protocol": {
                    "codec": CODEC_NAME,
                    "version": PROTOCOL_VERSION,
                    "supported_codecs": [CODEC_NAME],
                },
                "capabilities": {
                    "health": {"available": true},
                    "ping": {"available": true},
                    "check_drc": {
                        "available": false,
                        "reason": "DRC is not implemented by this worker scaffold."
                    }
                }
            }),
        )),
        "ping" => Ok(envelope(
            &request,
            "response",
            json!({"echo": Value::Object(request.payload.clone())}),
        )),
        "check_drc" => Ok(error_response(
            &request,
            "capability_unavailable",
            "DRC is not implemented by this worker backend.",
            json!({"capability": "check_drc", "backend": "rust"}),
        )),
        method => Ok(error_response(
            &request,
            "unsupported_method",
            &format!("Worker method {method:?} is not supported."),
            json!({"method": method}),
        )),
    }
}

fn read_frame(reader: &mut impl Read) -> io::Result<Option<Value>> {
    let mut header = [0_u8; 4];
    match reader.read(&mut header[..1])? {
        0 => return Ok(None),
        1 => {}
        _ => unreachable!(),
    }
    reader.read_exact(&mut header[1..])?;
    let body_length = u32::from_be_bytes(header) as usize;
    if body_length == 0 || body_length > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("invalid frame body length {body_length}"),
        ));
    }
    let mut body = vec![0_u8; body_length];
    reader.read_exact(&mut body)?;
    rmp_serde::from_slice(&body)
        .map(Some)
        .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))
}

fn write_frame(writer: &mut impl Write, value: &Value) -> io::Result<()> {
    let body = rmp_serde::to_vec_named(value)
        .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    if body.is_empty() || body.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("response frame is {} bytes", body.len()),
        ));
    }
    let length =
        u32::try_from(body.len()).map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    writer.write_all(&length.to_be_bytes())?;
    writer.write_all(&body)?;
    writer.flush()
}

fn serve() -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = stdin.lock();
    let mut writer = stdout.lock();
    let mut latest_revision = None;
    while let Some(value) = read_frame(&mut reader)? {
        let response = handle(&value, &mut latest_revision)
            .map_err(|message| io::Error::new(ErrorKind::InvalidData, message))?;
        write_frame(&mut writer, &response)?;
    }
    Ok(())
}

fn main() {
    let valid_args = std::env::args()
        .skip(1)
        .all(|argument| argument == "--serve");
    if !valid_args {
        eprintln!("usage: smd-twin-eda-core [--serve]");
        std::process::exit(2);
    }
    if let Err(error) = serve() {
        eprintln!("EDA worker protocol failure: {error}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(method: &str, revision: u64, payload: Value) -> Value {
        json!({
            "codec": CODEC_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "type": "request",
            "request_id": "test-request",
            "document_revision": revision,
            "method": method,
            "payload": payload,
        })
    }

    #[test]
    fn health_reports_real_capabilities() {
        let response = handle(&request("health", 0, json!({})), &mut None).unwrap();
        assert_eq!(response["type"], "response");
        assert_eq!(response["payload"]["backend"], "rust");
        assert_eq!(
            response["payload"]["capabilities"]["check_drc"]["available"],
            false
        );
    }

    #[test]
    fn drc_is_an_explicit_capability_error() {
        let response = handle(&request("check_drc", 3, json!({})), &mut None).unwrap();
        assert_eq!(response["type"], "error");
        assert_eq!(response["payload"]["code"], "capability_unavailable");
    }

    #[test]
    fn older_revision_is_rejected() {
        let mut latest = Some(5);
        let response = handle(&request("ping", 4, json!({})), &mut latest).unwrap();
        assert_eq!(response["payload"]["code"], "stale_revision");
        assert_eq!(latest, Some(5));
    }

    #[test]
    fn frame_round_trip_is_big_endian_and_bounded() {
        let value = request("ping", 0, json!({"value": 1}));
        let mut encoded = Vec::new();
        write_frame(&mut encoded, &value).unwrap();
        let declared = u32::from_be_bytes(encoded[..4].try_into().unwrap()) as usize;
        assert_eq!(declared, encoded.len() - 4);
        assert_eq!(read_frame(&mut encoded.as_slice()).unwrap(), Some(value));
    }
}
