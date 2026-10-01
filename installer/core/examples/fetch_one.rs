//! Dev tool (#196 phase 2 end-to-end): fetch one URL the way the installer
//! does (`fetch::download`, production settings), printing every event.
//!
//! `cargo run -p crowsetup-core --example fetch_one -- <url> <sha256> <bytes> <dest> [--pause-at <n>] [--stop-at <n>]`
//!
//! The state lives beside the file (`<dest>.state.json`), so killing the
//! process and starting the same command again resumes from the last
//! checkpoint, as the installer does from `setup\state.json`.
//! `--pause-at`: at that byte set the cancel flag (what Pause does in the run),
//! wait 3 s, then call `download` again with the same state (what Resume does).
//! `--stop-at`: at that byte cancel and exit 3, leaving the `.part`.

use crowsetup_core::api::{Event, FileJob, FileKind, Source};
use crowsetup_core::fetch::{self, FetchError, FetchOptions};
use crowsetup_core::state::StateStore;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

fn num(s: &str, what: &str) -> u64 {
    s.parse().unwrap_or_else(|_| {
        eprintln!("{what}: not a number: {s}");
        std::process::exit(2);
    })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (pos, flags) = args.split_at(args.len().min(4));
    let [url, sha256, bytes, dest] = pos else {
        eprintln!("usage: fetch_one <url> <sha256> <bytes> <dest> [--pause-at <n>] [--stop-at <n>]");
        std::process::exit(2);
    };
    let (mut pause_at, mut stop_at) = (None, None);
    for pair in flags.chunks(2) {
        match pair {
            [f, v] if f == "--pause-at" => pause_at = Some(num(v, f)),
            [f, v] if f == "--stop-at" => stop_at = Some(num(v, f)),
            _ => {
                eprintln!("unknown option: {pair:?}");
                std::process::exit(2);
            }
        }
    }
    let bytes = num(bytes, "bytes");
    let dest = PathBuf::from(dest);
    let mut state_path = dest.clone().into_os_string();
    state_path.push(".state.json");
    let mut state = StateStore::load(&PathBuf::from(state_path)).expect("state file");
    let job = FileJob {
        id: "one".into(),
        kind: FileKind::Model,
        url: url.clone(),
        local_rel: String::new(),
        dest,
        bytes,
        sha256: sha256.to_ascii_lowercase(),
        points: vec![],
    };
    let cancel = AtomicBool::new(false);
    let stopping = AtomicBool::new(false);
    let t0 = Instant::now();
    let mut last = (t0, 0u64);
    let mut on = |e: Event| {
        let t = t0.elapsed().as_secs_f64();
        match &e {
            Event::FileProgress { done, total, .. } => {
                if pause_at.is_some_and(|p| *done >= p) {
                    pause_at = None;
                    println!("{t:8.1}s PAUSE at byte {done}");
                    cancel.store(true, Ordering::SeqCst);
                }
                if stop_at.is_some_and(|p| *done >= p) {
                    println!("{t:8.1}s STOP at byte {done}");
                    stopping.store(true, Ordering::SeqCst);
                    cancel.store(true, Ordering::SeqCst);
                }
                // one line per 2 s, with the rate since the last line
                let now = Instant::now();
                let secs = now.duration_since(last.0).as_secs_f64();
                if secs >= 2.0 || done == total {
                    let mbs = (*done - last.1.min(*done)) as f64 / 1e6 / secs.max(1e-3);
                    println!("{t:8.1}s progress {done}/{total} ({:.1} %) {mbs:.1} MB/s", *done as f64 * 100.0 / *total as f64);
                    last = (now, *done);
                }
            }
            Event::FileStarted { from_byte, .. } => {
                last = (Instant::now(), *from_byte);
                println!("{t:8.1}s {}", serde_json::to_string(&e).unwrap());
            }
            _ => println!("{t:8.1}s {}", serde_json::to_string(&e).unwrap()),
        }
    };
    let opts = FetchOptions::production(Source::Remote);
    let r = loop {
        match fetch::download(&job, &mut state, &opts, &mut on, &cancel) {
            Err(FetchError::Cancelled) if !stopping.load(Ordering::SeqCst) => {
                let at = state.files.get("one").map_or(0, |f| f.bytes_done);
                println!("{:8.1}s paused, state bytes_done {at}; resuming in 3 s", t0.elapsed().as_secs_f64());
                std::thread::sleep(Duration::from_secs(3));
                cancel.store(false, Ordering::SeqCst);
            }
            other => break other,
        }
    };
    let secs = t0.elapsed().as_secs_f64();
    match r {
        Ok(()) => println!("OK in {secs:.1} s"),
        Err(FetchError::Cancelled) => {
            let at = state.files.get("one").map_or(0, |f| f.bytes_done);
            println!("stopped after {secs:.1} s, state bytes_done {at}");
            std::process::exit(3);
        }
        Err(e) => {
            println!("ERROR after {secs:.1} s: {e}");
            std::process::exit(1);
        }
    }
}
