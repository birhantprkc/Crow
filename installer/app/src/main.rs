//! CrowSetup.exe (#196 phase 2): the window (tao + wry over the system
//! WebView2), `--headless`, `--selftest`. Everything it installs is
//! crowsetup-core's run; this crate is the face and the command line.

// A release build is a GUI-subsystem exe (no console flashes up on a double
// click); --headless / --selftest attach to the parent console, and a
// redirected stdout (CI, build.ps1) works either way.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bundle;
mod cli;
mod folders;
mod headless;
mod selftest;
mod window;

use crowsetup_core::api::{Packages, Selection, Source};
use crowsetup_core::run::{RealSteps, RunOptions, default_install_root, state_path};
use std::path::PathBuf;

/// Everything a real run needs, from the command line and the bundle.
pub struct Setup {
    pub source: Source,
    pub packages: Packages,
    pub opts: RunOptions,
    pub desktop: Option<PathBuf>,
}

impl Setup {
    pub fn steps(&self) -> RealSteps {
        RealSteps::new(self.source.clone(), self.packages.clone(), bundle::PYTHON_ZIP, bundle::GET_PIP)
    }
}

fn attach_console() {
    #[cfg(windows)]
    // SAFETY: plain Win32 call; failing (no parent console) is fine.
    unsafe {
        windows_sys::Win32::System::Console::AttachConsole(windows_sys::Win32::System::Console::ATTACH_PARENT_PROCESS);
    }
}

fn load_packages(path: Option<&PathBuf>) -> Result<Packages, String> {
    let text = match path {
        Some(p) => std::fs::read_to_string(p).map_err(|e| format!("--packages {}: {e}", p.display()))?,
        None => bundle::PACKAGES_JSON
            .ok_or("--packages <file> is required (this build does not carry packages.json)")?
            .to_string(),
    };
    serde_json::from_str(&text).map_err(|e| format!("packages.json: {e}"))
}

fn setup(args: &cli::Args) -> Result<Setup, String> {
    let packages = load_packages(args.packages.as_ref())?;
    let source = match &args.source {
        Some(d) if d.is_dir() => Source::Local(d.clone()),
        Some(d) => return Err(format!("--source {}: not a folder", d.display())),
        None => Source::Remote,
    };
    let launch_root = args.install_root.clone().unwrap_or_else(default_install_root);
    let opts = RunOptions {
        state_path: state_path(&launch_root),
        launch_root,
        start_menu_dir: folders::start_menu_programs(),
    };
    Ok(Setup { source, packages, opts, desktop: folders::desktop() })
}

fn fail(msg: &str) -> i32 {
    eprintln!("crowsetup: {msg}");
    2
}

fn real_main() -> i32 {
    let args = match cli::parse(std::env::args().skip(1)) {
        Ok(a) => a,
        Err(e) => {
            attach_console();
            return fail(&format!("{e}\n\n{}", cli::USAGE));
        }
    };
    match args.mode {
        cli::Mode::Help => {
            attach_console();
            println!("{}", cli::USAGE);
            0
        }
        cli::Mode::Selftest => {
            attach_console();
            selftest::main()
        }
        cli::Mode::Headless => {
            attach_console();
            let s = match setup(&args) {
                Ok(s) => s,
                Err(e) => return fail(&e),
            };
            let sel = (!args.points.is_empty()).then(|| Selection {
                points: args.points.clone(),
                install_root: s.opts.launch_root.clone(),
                shortcut_dir: s.desktop.clone(),
            });
            headless::main(&mut s.steps(), &s.opts, sel)
        }
        cli::Mode::Window => match setup(&args) {
            Ok(s) => window::main(s),
            Err(e) => {
                attach_console();
                window::message_box(&e);
                fail(&e)
            }
        },
    }
}

fn main() {
    std::process::exit(real_main());
}
