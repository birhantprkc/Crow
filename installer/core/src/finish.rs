//! T3: shortcuts through `crow_boot.py --create-shortcut` and the operating-point window.
//!
//! The shortcut is crow_boot's own (`crow_boot.py --create-shortcut DIR`: since
//! #196's operating-point window, `pythonw.exe` beside the Python running
//! `crow_boot.py --gui`, so no console opens; working folder the install root,
//! cli/crow.ico), written once into each folder the run passes -- the run adds
//! the Start menu (`%APPDATA%\Microsoft\Windows\Start Menu\Programs`) when
//! it is asked to, nothing here adds a folder behind its back (#196 P2-E2E).
//! `--install-root <root>` is added only when the root is not the default
//! `%LOCALAPPDATA%\Crow`; `--models <root>\models` always, because the
//! installer always puts the models there and `$CROW_MODELS` (a lab root for
//! the llama.cpp lines) must not point the boot menu elsewhere. crow_boot
//! carries both into the shortcut's arguments.
//!
//! `open_boot_menu` opens that window the way the shortcut does: `pythonw.exe
//! <crow_boot.py> --gui [--install-root <root>] --models <root>\models`, or the console Python without a
//! console window when no pythonw.exe sits beside it. The terminal menu stays
//! `python cli\crow_boot.py`.

use crate::python::{output_tail, quiet, PythonInfo};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn default_install_root() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(|l| PathBuf::from(l).join("Crow"))
}

pub fn start_menu_dir() -> Option<PathBuf> {
    std::env::var_os("APPDATA")
        .map(|a| PathBuf::from(a).join("Microsoft").join("Windows").join("Start Menu").join("Programs"))
}

/// Equal as NTFS sees it: case and separators ignored, a trailing separator too.
pub fn same_path(a: &Path, b: &Path) -> bool {
    let norm = |p: &Path| p.to_string_lossy().replace('/', "\\").trim_end_matches('\\').to_lowercase();
    norm(a) == norm(b)
}

/// The chosen folders plus the Start menu, each once, in order.
pub fn shortcut_dirs(dirs: &[PathBuf], start_menu: Option<PathBuf>) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for d in dirs.iter().cloned().chain(start_menu) {
        if !out.iter().any(|o| same_path(o, &d)) {
            out.push(d);
        }
    }
    out
}

fn root_flag(install_root: &Path, default_root: Option<&Path>) -> Vec<OsString> {
    match default_root {
        Some(d) if same_path(d, install_root) => vec![],
        _ => vec!["--install-root".into(), install_root.as_os_str().to_os_string()],
    }
}

/// Arguments for the Python that writes one shortcut.
pub fn shortcut_args(script: &Path, install_root: &Path, dir: &Path, default_root: Option<&Path>) -> Vec<OsString> {
    let mut args: Vec<OsString> = vec![script.as_os_str().to_os_string(), "--create-shortcut".into(), dir.as_os_str().to_os_string()];
    args.extend(root_flag(install_root, default_root));
    args
}

/// `--models <install>\models`: added to every crow_boot call the installer makes.
pub fn models_flag(install_root: &Path) -> Vec<OsString> {
    vec!["--models".into(), install_root.join("models").into_os_string()]
}

/// `pythonw.exe` beside `python` when it is there (the window needs no console), else `python`.
pub fn windowed_python(python: &Path) -> PathBuf {
    match python.parent().map(|d| d.join("pythonw.exe")) {
        Some(w) if w.is_file() => w,
        _ => python.to_path_buf(),
    }
}

/// Program and arguments that open the operating-point window like its shortcut.
pub fn window_command(python: &Path, script: &Path, install_root: &Path, default_root: Option<&Path>) -> (PathBuf, Vec<OsString>) {
    let mut args: Vec<OsString> = vec![script.as_os_str().to_os_string(), "--gui".into()];
    args.extend(root_flag(install_root, default_root));
    (windowed_python(python), args)
}

fn boot_script(install_root: &Path) -> Result<PathBuf, String> {
    let script = install_root.join("cli").join("crow_boot.py");
    if script.is_file() {
        Ok(script)
    } else {
        Err(format!("{} is missing; Crow's package is not installed", script.display()))
    }
}

pub fn shortcuts(py: &PythonInfo, install_root: &Path, dirs: &[PathBuf]) -> Result<(), String> {
    let script = boot_script(install_root)?;
    let default = default_install_root();
    let mut failed = Vec::new();
    for dir in shortcut_dirs(dirs, None) {
        let out = quiet(&py.exe)
            .args(shortcut_args(&script, install_root, &dir, default.as_deref()))
            .args(models_flag(install_root))
            .current_dir(install_root)
            .output();
        match out {
            Ok(o) if o.status.success() => {}
            Ok(o) => failed.push(format!("{}: {}", dir.display(), output_tail(&o, 2))),
            Err(e) => failed.push(format!("{}: {e}", dir.display())),
        }
    }
    if failed.is_empty() {
        Ok(())
    } else {
        Err(format!("shortcut not written: {}", failed.join("; ")))
    }
}

pub fn open_boot_menu(py: &PythonInfo, install_root: &Path) -> Result<(), String> {
    let script = boot_script(install_root)?;
    let (program, args) = window_command(&py.exe, &script, install_root, default_install_root().as_deref());
    let mut cmd = Command::new(&program);
    cmd.args(&args).args(models_flag(install_root)).current_dir(install_root);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // a window, not a console: when python.exe stands in for a missing
        // pythonw.exe, no console window opens beside it
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    // not waited for: the window outlives the installer
    cmd.spawn().map(drop).map_err(|e| format!("cannot open the boot menu ({}): {e}", program.display()))
}
