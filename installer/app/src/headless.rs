//! `--headless`: the same run, events as plain text lines on stdout.

use crowsetup_core::api::{Command, Event, Selection, StepStatus};
use crowsetup_core::run::{self, Input, Outcome, RunOptions, Steps, gb};
use std::collections::HashMap;
use std::time::{Duration, Instant};

/// Exit code: 0 Done, 1 Fatal, 2 Quit / nothing to do.
pub fn main(steps: &mut dyn Steps, opts: &RunOptions, sel: Option<Selection>) -> i32 {
    let (tx, rx) = std::sync::mpsc::channel();
    match &sel {
        Some(s) => tx.send(Input::Command(Command::Start(s.clone()))).unwrap(),
        None => tx.send(Input::Command(Command::Resume)).unwrap(),
    }
    // No one can send Retry or Resume later: a failure ends the run.
    drop(tx);
    let mut printer = Printer::default();
    let out = run::run(steps, opts, &mut |e| printer.print(&e), rx);
    match out {
        Outcome::Done => 0,
        Outcome::Fatal(_) => 1,
        Outcome::Quit => {
            if !printer.planned {
                println!("Nothing to continue. Pass --points (flash-next, 27b, image-stack, media-stack).");
            }
            2
        }
    }
}

#[derive(Default)]
struct Printer {
    planned: bool,
    last: HashMap<String, (Instant, u64)>,
}

impl Printer {
    fn print(&mut self, e: &Event) {
        match e {
            Event::Preflight(r) => {
                println!(
                    "Machine: {} ({} MiB, compute {}), RAM {}, free disk {}",
                    r.gpu_name.as_deref().unwrap_or("no NVIDIA GPU"),
                    r.vram_mib.unwrap_or(0),
                    r.compute_cap.as_deref().unwrap_or("?"),
                    gb(r.ram_bytes),
                    gb(r.disk_free_bytes)
                );
                if let Some(h) = &r.hard_block {
                    println!("Blocked: {h}");
                }
                for b in &r.blocked {
                    println!("Not on this machine: {}: {}", b.point, b.reason);
                }
            }
            Event::Planned(p) => {
                self.planned = true;
                println!(
                    "Plan: {} files, {} to download, {} on disk.",
                    p.jobs.len(),
                    gb(p.download_bytes),
                    gb(p.disk_bytes)
                );
            }
            Event::Checking { id, bytes } => println!("{id}: checking what is already here ({})", gb(*bytes)),
            Event::FileStarted { id, from_byte, total } => {
                if *from_byte > 0 {
                    println!("{id}: continuing at {} of {}", gb(*from_byte), gb(*total));
                } else {
                    println!("{id}: {}", gb(*total));
                }
            }
            Event::FileProgress { id, done, total } => {
                // At most every 5 s per file, and at the end.
                let now = Instant::now();
                let due = self.last.get(id).is_none_or(|(t, _)| now.duration_since(*t) >= Duration::from_secs(5));
                if due || done == total {
                    self.last.insert(id.clone(), (now, *done));
                    let pct = if *total > 0 { *done as f64 * 100.0 / *total as f64 } else { 100.0 };
                    println!("{id}: {} of {} ({pct:.0} %)", gb(*done), gb(*total));
                }
            }
            Event::FileRetry { id, attempt, reason } => println!("{id}: reconnecting, attempt {attempt}: {reason}"),
            Event::FileVerified { id } => println!("{id}: verified"),
            Event::FileError { id, message, .. } => println!("{id}: ERROR {message}"),
            Event::Step { name, status, detail } => {
                let s = match status {
                    StepStatus::Running => "...",
                    StepStatus::Ok => "ok",
                    StepStatus::Warning => "WARNING",
                    StepStatus::Failed => "FAILED",
                };
                println!("[{name}] {s} {detail}");
            }
            Event::Done { installed, shortcut } => {
                println!("Landed. Crow is ready. Installed: {}.", installed.join(", "));
                if let Some(s) = shortcut {
                    println!("Shortcut: {}", s.display());
                }
            }
            Event::Fatal { message } => println!("FATAL: {message}"),
        }
    }
}
