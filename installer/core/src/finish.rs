//! T3: shortcuts through `crow_boot.py --create-shortcut` and the boot menu launch.
//!
//! The shortcut is the boot menu's own (`crow_boot.py --create-shortcut DIR`:
//! Windows Terminal when there is one, the console Python otherwise, working
//! folder the install root, cli/crow.ico), written once into each chosen folder
//! and always into the Start menu (`%APPDATA%\Microsoft\Windows\Start Menu\Programs`).
//! `--install-root <root>` is added only when the root is not the default
//! `%LOCALAPPDATA%\Crow`; crow_boot then carries it into the shortcut's arguments.
//!
//! `open_boot_menu` starts the menu the way that shortcut does: `wt.exe --title
//! Crow -d <root> <python> <crow_boot.py> [--install-root <root>]` when Windows
//! Terminal is on PATH, else the console Python in a console of its own.

use crate::python::{output_tail, quiet, PythonInfo};
use std::ffi::{OsStr, OsString};
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

/// The first `name` on `path_var`. WindowsApps is searched too: `wt.exe` lives there.
pub fn find_on_path(name: &str, path_var: &OsStr) -> Option<PathBuf> {
    std::env::split_paths(path_var)
        .filter(|d| !d.as_os_str().is_empty())
        .map(|d| d.join(name))
        .find(|p| p.is_file())
}

/// Program and arguments that open the boot menu like its shortcut.
pub fn menu_command(
    python: &Path,
    script: &Path,
    install_root: &Path,
    wt: Option<&Path>,
    default_root: Option<&Path>,
) -> (PathBuf, Vec<OsString>) {
    let mut tail: Vec<OsString> = vec![script.as_os_str().to_os_string()];
    tail.extend(root_flag(install_root, default_root));
    match wt {
        Some(wt) => {
            let mut args: Vec<OsString> =
                vec!["--title".into(), "Crow".into(), "-d".into(), install_root.as_os_str().to_os_string(), python.as_os_str().to_os_string()];
            args.extend(tail);
            (wt.to_path_buf(), args)
        }
        None => (python.to_path_buf(), tail),
    }
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
    for dir in shortcut_dirs(dirs, start_menu_dir()) {
        let out = quiet(&py.exe)
            .args(shortcut_args(&script, install_root, &dir, default.as_deref()))
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
    let path = std::env::var_os("PATH").unwrap_or_default();
    let wt = find_on_path("wt.exe", &path);
    let (program, args) = menu_command(&py.exe, &script, install_root, wt.as_deref(), default_install_root().as_deref());
    let mut cmd = Command::new(&program);
    cmd.args(&args).current_dir(install_root);
    #[cfg(windows)]
    if wt.is_none() {
        use std::os::windows::process::CommandExt;
        // the installer has no console; the menu needs one of its own
        const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;
        cmd.creation_flags(CREATE_NEW_CONSOLE);
    }
    // not waited for: the menu outlives the installer
    cmd.spawn().map(drop).map_err(|e| format!("cannot open the boot menu ({}): {e}", program.display()))
}
