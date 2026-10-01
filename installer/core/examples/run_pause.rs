//! Dev tool (#196 phase 2 end-to-end): the real run (`run::run` on
//! `RealSteps`) from a local `--source` folder, with Pause, Resume and Quit
//! sent over the command channel the way the window sends them.
//!
//! `cargo run -p crowsetup-core --example run_pause -- <source dir> <packages.json> <install root> <point> <file id> <pause at> <quit at>`
//!
//! When `<file id>` passes `<pause at>` bytes: Pause, then Resume 3 s later.
//! When it passes `<quit at>`: Quit (the window's X). No shortcut folder and no
//! Start menu folder are given, so nothing outside the install root is written.

use crowsetup_core::api::{Command, Event, Packages, Selection, Source};
use crowsetup_core::run::{self, Input, RealSteps, RunOptions};
use std::path::PathBuf;
use std::time::{Duration, Instant};

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let [src, packages, root, point, id, pause_at, quit_at] = a.as_slice() else {
        eprintln!("usage: run_pause <source dir> <packages.json> <install root> <point> <file id> <pause at> <quit at>");
        std::process::exit(2);
    };
    let packages: Packages = serde_json::from_str(&std::fs::read_to_string(packages).expect("packages.json")).expect("packages.json");
    let (mut pause_at, quit_at): (Option<u64>, u64) = (Some(pause_at.parse().unwrap()), quit_at.parse().unwrap());
    let root = PathBuf::from(root);
    let opts = RunOptions { state_path: run::state_path(&root), launch_root: root.clone(), start_menu_dir: None };
    let sel = Selection { points: vec![point.clone()], install_root: root, shortcut_dir: None };
    let mut steps = RealSteps::new(Source::Local(PathBuf::from(src)), packages, None, None);
    let (tx, rx) = std::sync::mpsc::channel();
    tx.send(Input::Command(Command::Start(sel))).unwrap();
    let t0 = Instant::now();
    let mut last = t0;
    let mut quit_sent = false;
    let tx2 = tx.clone();
    let mut on = |e: Event| {
        let t = t0.elapsed().as_secs_f64();
        match &e {
            Event::FileProgress { id: fid, done, total } => {
                if fid == id && pause_at.is_some_and(|p| *done >= p) {
                    pause_at = None;
                    println!("{t:8.1}s -> Pause ({fid} at byte {done})");
                    tx2.send(Input::Command(Command::Pause)).unwrap();
                    let tx3 = tx2.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_secs(3));
                        println!("          -> Resume");
                        let _ = tx3.send(Input::Command(Command::Resume));
                    });
                }
                if fid == id && *done >= quit_at && !quit_sent {
                    quit_sent = true;
                    println!("{t:8.1}s -> Quit ({fid} at byte {done})");
                    tx2.send(Input::Command(Command::Quit)).unwrap();
                }
                if last.elapsed() >= Duration::from_secs(5) || done == total {
                    last = Instant::now();
                    println!("{t:8.1}s progress {fid} {done}/{total}");
                }
            }
            Event::Planned(p) => println!("{t:8.1}s planned {} jobs", p.jobs.len()),
            Event::Preflight(_) => println!("{t:8.1}s preflight"),
            Event::Done { .. } => {
                println!("{t:8.1}s {}", serde_json::to_string(&e).unwrap());
                // after Done the run serves the Landed screen until Quit
                let _ = tx2.send(Input::Command(Command::Quit));
            }
            _ => println!("{t:8.1}s {}", serde_json::to_string(&e).unwrap()),
        }
    };
    let out = run::run(&mut steps, &opts, &mut on, rx);
    drop(tx);
    println!("{:8.1}s outcome {out:?}", t0.elapsed().as_secs_f64());
}
