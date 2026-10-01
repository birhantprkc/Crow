//! #196 P2-E2E: on robin's machine Hugging Face over IPv6 accepts the TCP
//! connection and then resets it (os error 10054, 15 of 15 attempts), while
//! IPv4 answers. ureq tries the next address only when a CONNECT fails, not
//! when an accepted connection is reset, so the fetcher itself must fall back
//! to IPv4 after a failed request and keep the family that worked.
//!
//! The seam: `localhost` resolves to `::1` first, then `127.0.0.1` (checked on
//! the Windows build machine). The file is served on `127.0.0.1:P`; on `[::1]:P`
//! a listener accepts every connection and drops it unread, the reset.

mod fetch_support;

use crowsetup_core::api::Source;
use crowsetup_core::fetch::download;
use crowsetup_core::state::StateStore;
use fetch_support::*;
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

#[test]
fn a_reset_over_ipv6_falls_back_to_ipv4() {
    let resolves: Vec<_> = std::net::ToSocketAddrs::to_socket_addrs("localhost:80").unwrap().collect();
    if !(resolves.first().is_some_and(|a| a.is_ipv6()) && resolves.iter().any(|a| a.is_ipv4())) {
        eprintln!("localhost does not resolve to ::1 first and 127.0.0.1 here ({resolves:?}); the seam does not apply");
        return;
    }
    let content = data(300_000, 51);
    // the IPv4 server, and the resetting IPv6 listener on the same port
    let (srv, v6) = loop {
        let c = content.clone();
        let srv = TestServer::start(move |_, seen, rq| serve_range(seen, rq, &c, "\"f\""));
        let port: u16 = srv.base.rsplit(':').next().unwrap().parse().unwrap();
        if let Ok(l) = TcpListener::bind(("::1", port)) {
            break (srv, l);
        }
    };
    let resets = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let r2 = resets.clone();
    std::thread::spawn(move || {
        for s in v6.incoming().flatten() {
            r2.fetch_add(1, Ordering::SeqCst);
            drop(s);
        }
    });
    let port = srv.base.rsplit(':').next().unwrap().to_string();
    let tmp = tempfile::tempdir().unwrap();
    let dest = tmp.path().join("f.bin");
    let job = job("f", &format!("http://localhost:{port}/f.bin"), &dest, &content);
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();

    // Without a fallback the fetcher retries the reset forever: give it 10 s.
    let cancel = Arc::new(AtomicBool::new(false));
    let c2 = cancel.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        c2.store(true, Ordering::SeqCst);
    });
    let mut ev = Vec::new();
    let r = download(&job, &mut st, &opts(Source::Remote), &mut |e| ev.push(e), &cancel);
    assert!(r.is_ok(), "the download did not get through: {r:?}, after {} resets", resets.load(Ordering::SeqCst));
    assert_eq!(std::fs::read(&dest).unwrap(), content);
    assert!(resets.load(Ordering::SeqCst) >= 1, "the IPv6 listener was never tried");
    assert!(retries(&ev).iter().any(|r| r.contains("IPv4")), "{:?}", retries(&ev));
}
