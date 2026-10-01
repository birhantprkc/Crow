//! Shared helpers for the T1 fetch tests: an in-process tiny_http server that
//! logs every request and lets each test decide how to answer.
#![allow(dead_code)]

use crowsetup_core::api::{Event, FileJob, FileKind, Source};
use crowsetup_core::fetch::FetchOptions;
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tiny_http::{Header, Request, Response, Server, StatusCode};

/// What a request carried, as the server saw it.
#[derive(Debug, Clone)]
pub struct Seen {
    pub url: String,
    pub range: Option<String>,
    pub if_range: Option<String>,
}

type Handler = dyn Fn(usize, &Seen, Request) + Send + Sync;

pub struct TestServer {
    pub base: String,
    pub log: Arc<Mutex<Vec<Seen>>>,
    server: Arc<Server>,
}

impl TestServer {
    /// `handler(n, seen, request)`: `n` counts requests from 0.
    pub fn start(handler: impl Fn(usize, &Seen, Request) + Send + Sync + 'static) -> TestServer {
        let server = Arc::new(Server::http("127.0.0.1:0").expect("bind"));
        let base = format!("http://{}", server.server_addr().to_ip().expect("ip addr"));
        let log: Arc<Mutex<Vec<Seen>>> = Arc::default();
        let handler: Arc<Handler> = Arc::new(handler);
        let (srv, lg) = (server.clone(), log.clone());
        thread::spawn(move || {
            while let Ok(rq) = srv.recv() {
                let seen = Seen {
                    url: rq.url().to_string(),
                    range: header(&rq, "Range"),
                    if_range: header(&rq, "If-Range"),
                };
                let n = {
                    let mut l = lg.lock().unwrap();
                    l.push(seen.clone());
                    l.len() - 1
                };
                let h = handler.clone();
                thread::spawn(move || h(n, &seen, rq));
            }
        });
        TestServer { base, log, server }
    }

    pub fn seen(&self) -> Vec<Seen> {
        self.log.lock().unwrap().clone()
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.server.unblock();
    }
}

fn header(rq: &Request, name: &str) -> Option<String> {
    rq.headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(name))
        .map(|h| h.value.as_str().to_string())
}

pub fn hdr(name: &str, value: &str) -> Header {
    Header::from_bytes(name.as_bytes(), value.as_bytes()).unwrap()
}

/// Offset of a `Range: bytes=<n>-` header.
pub fn range_start(seen: &Seen) -> Option<u64> {
    seen.range
        .as_deref()?
        .strip_prefix("bytes=")?
        .trim_end_matches('-')
        .parse()
        .ok()
}

/// A correct Range server: 206 for `bytes=n-`, 200 when `If-Range` does not
/// match `etag`, 416 when `n` is at or past the end.
pub fn serve_range(seen: &Seen, rq: Request, data: &[u8], etag: &str) {
    let start = range_start(seen);
    let if_range_ok = seen.if_range.as_deref().is_none_or(|v| v == etag);
    let resp = match start {
        Some(n) if if_range_ok && n >= data.len() as u64 => Response::from_data(Vec::new())
            .with_status_code(StatusCode(416))
            .with_header(hdr("Content-Range", &format!("bytes */{}", data.len()))),
        Some(n) if if_range_ok => Response::from_data(data[n as usize..].to_vec())
            .with_status_code(StatusCode(206))
            .with_header(hdr(
                "Content-Range",
                &format!("bytes {}-{}/{}", n, data.len() - 1, data.len()),
            )),
        _ => Response::from_data(data.to_vec()).with_status_code(StatusCode(200)),
    };
    let _ = rq.respond(
        resp.with_header(hdr("ETag", etag))
            .with_header(hdr("Accept-Ranges", "bytes")),
    );
}

/// Answer `status` with `body` as one chunk, wait `pause` with the socket
/// open, then break the chunked framing: the client sees bytes, then (after
/// the pause) a broken connection mid-body. `extra`: more header lines, each
/// ending in CRLF. (tiny_http 0.12 cannot close a keep-alive socket from a
/// handler, so a broken chunk stands in for a TCP reset; the client gets a
/// read error either way.)
pub fn serve_broken(
    rq: Request,
    status: u16,
    etag: &str,
    extra: &str,
    body: &[u8],
    pause: Duration,
) {
    let mut w = rq.into_writer();
    let head = format!(
        "HTTP/1.1 {status} X\r\nETag: {etag}\r\n{extra}Transfer-Encoding: chunked\r\nContent-Type: application/octet-stream\r\n\r\n"
    );
    let _ = w.write_all(head.as_bytes());
    let _ = write!(w, "{:x}\r\n", body.len());
    let _ = w.write_all(body);
    let _ = w.write_all(b"\r\n");
    let _ = w.flush();
    thread::sleep(pause);
    let _ = w.write_all(b"zz-not-a-chunk\r\n\r\n");
    let _ = w.flush();
}

/// A reader that hands out `step` bytes per read with `delay` between reads.
pub struct Slow {
    pub data: Vec<u8>,
    pub pos: usize,
    pub step: usize,
    pub delay: Duration,
}

impl Read for Slow {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.pos >= self.data.len() {
            return Ok(0);
        }
        thread::sleep(self.delay);
        let n = self.step.min(buf.len()).min(self.data.len() - self.pos);
        buf[..n].copy_from_slice(&self.data[self.pos..self.pos + n]);
        self.pos += n;
        Ok(n)
    }
}

/// Deterministic test bytes.
pub fn data(len: usize, seed: u32) -> Vec<u8> {
    let mut x = seed.wrapping_mul(2654435761).wrapping_add(1);
    (0..len)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            (x >> 24) as u8
        })
        .collect()
}

pub fn sha_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub fn job(id: &str, url: &str, dest: &Path, content: &[u8]) -> FileJob {
    FileJob {
        id: id.to_string(),
        kind: FileKind::Model,
        url: url.to_string(),
        local_rel: format!("repo/{id}.bin"),
        dest: dest.to_path_buf(),
        bytes: content.len() as u64,
        sha256: sha_hex(content),
        points: vec!["27b".to_string()],
    }
}

pub fn opts(source: Source) -> FetchOptions {
    FetchOptions {
        source,
        stall_secs: 5,
        checkpoint_bytes: 4096,
        max_backoff_secs: 0,
    }
}

pub fn part_of(dest: &Path) -> std::path::PathBuf {
    let mut s = dest.as_os_str().to_owned();
    s.push(".part");
    s.into()
}

pub fn retries(events: &[Event]) -> Vec<String> {
    events
        .iter()
        .filter_map(|e| match e {
            Event::FileRetry { reason, .. } => Some(reason.clone()),
            _ => None,
        })
        .collect()
}

pub fn verified(events: &[Event], id: &str) -> bool {
    events
        .iter()
        .any(|e| matches!(e, Event::FileVerified { id: i } if i == id))
}

pub fn checking(events: &[Event]) -> Option<u64> {
    events.iter().find_map(|e| match e {
        Event::Checking { bytes, .. } => Some(*bytes),
        _ => None,
    })
}
