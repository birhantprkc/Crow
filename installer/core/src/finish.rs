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
//!
//! LINUX (#342): crow_boot's `--create-shortcut` writes Windows `.lnk` files
//! only, so the shortcuts are written here as desktop entries (freedesktop
//! Desktop Entry Specification 1.5): `crow-operating-points.desktop` ("Crow
//! Operating Points": `<venv python> <root>/cli/crow_boot.py --gui
//! [--install-root <root>] --models <root>/models`) into every folder, and in the
//! launcher folder `$XDG_DATA_HOME/applications` also `crow.desktop` ("Crow":
//! `<venv python> <root>/cli/crow_gui.py`, install.sh's launcher and
//! cli/crow.desktop's `StartupWMClass=crow`). The default root is
//! `$XDG_DATA_HOME/crow` (install.sh's `CROW_HOME`). Icons are absolute paths to
//! `<root>/cli/icons/crow-256.png`, so no icon cache is involved.

use crate::python::PythonInfo;
#[cfg(windows)]
use crate::python::{output_tail, quiet};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(windows)]
pub fn default_install_root() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(|l| PathBuf::from(l).join("Crow"))
}

/// `$XDG_DATA_HOME/crow` (install.sh's `CROW_HOME` default).
#[cfg(not(windows))]
pub fn default_install_root() -> Option<PathBuf> {
    xdg_data_home().map(|d| d.join("crow"))
}

/// `$XDG_DATA_HOME` when it is an absolute path (the spec ignores a relative
/// one), else `$HOME/.local/share`.
pub fn xdg_data_home() -> Option<PathBuf> {
    xdg_data_home_from(std::env::var_os("XDG_DATA_HOME"), std::env::var_os("HOME"))
}

pub fn xdg_data_home_from(xdg: Option<std::ffi::OsString>, home: Option<std::ffi::OsString>) -> Option<PathBuf> {
    match xdg.map(PathBuf::from) {
        Some(p) if p.is_absolute() => Some(p),
        _ => home.filter(|h| !h.is_empty()).map(|h| PathBuf::from(h).join(".local").join("share")),
    }
}

#[cfg(windows)]
pub fn start_menu_dir() -> Option<PathBuf> {
    std::env::var_os("APPDATA")
        .map(|a| PathBuf::from(a).join("Microsoft").join("Windows").join("Start Menu").join("Programs"))
}

/// The application launchers' folder, `$XDG_DATA_HOME/applications`.
#[cfg(not(windows))]
pub fn start_menu_dir() -> Option<PathBuf> {
    xdg_data_home().map(|d| d.join("applications"))
}

/// Equal as NTFS sees it: case and separators ignored, a trailing separator too.
#[cfg(windows)]
pub fn same_path(a: &Path, b: &Path) -> bool {
    let norm = |p: &Path| p.to_string_lossy().replace('/', "\\").trim_end_matches('\\').to_lowercase();
    norm(a) == norm(b)
}

/// Equal as a case-sensitive file system sees it; a trailing `/` is ignored.
#[cfg(not(windows))]
pub fn same_path(a: &Path, b: &Path) -> bool {
    let norm = |p: &Path| p.to_string_lossy().trim_end_matches('/').to_string();
    norm(a) == norm(b)
}

/// The boot menu shortcut `--create-shortcut` (Windows) or this module (Linux) writes into a folder.
pub const BOOT_SHORTCUT: &str = if cfg!(windows) { "Crow.lnk" } else { "crow-operating-points.desktop" };
/// Linux: the Crow window's launcher entry (the name install.sh uses too).
pub const CROW_SHORTCUT: &str = "crow.desktop";

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

/// One argument of an `Exec=` line: quoted when it holds a reserved character,
/// `"`, `` ` ``, `$` and `\` escaped inside the quotes (Desktop Entry Spec,
/// "The Exec key"). The string-level escaping is [`desktop_string`]'s.
pub fn desktop_quote(arg: &str) -> String {
    const RESERVED: &[char] = &[' ', '\t', '"', '\'', '\\', '>', '<', '~', '|', '&', ';', '$', '*', '?', '#', '(', ')', '`'];
    if !arg.is_empty() && !arg.contains(RESERVED) {
        return arg.to_string();
    }
    let mut out = String::from("\"");
    for c in arg.chars() {
        if matches!(c, '"' | '`' | '$' | '\\') {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('"');
    out
}

/// A value of type string: `\` doubled (the spec's escape rule for strings,
/// applied after the Exec quoting, hence four backslashes for one).
pub fn desktop_string(v: &str) -> String {
    v.replace('\\', "\\\\")
}

/// The `Exec=` value for `argv`: each argument quoted, the whole a string,
/// `%` written `%%` (field codes). A newline cannot be expressed: refused.
pub fn desktop_exec(argv: &[OsString]) -> Result<String, String> {
    let mut parts = Vec::new();
    for a in argv {
        let a = a.to_str().ok_or_else(|| format!("{} is not UTF-8", a.to_string_lossy()))?;
        if a.contains(['\n', '\r']) {
            return Err(format!("{a:?} holds a line break"));
        }
        parts.push(desktop_quote(a));
    }
    Ok(desktop_string(&parts.join(" ")).replace('%', "%%"))
}

/// One `[Desktop Entry]`: an application started without a terminal in `workdir`.
pub fn desktop_entry(
    name: &str,
    comment: &str,
    argv: &[OsString],
    workdir: &Path,
    icon: &str,
    wm_class: Option<&str>,
) -> Result<String, String> {
    let mut s = String::from("[Desktop Entry]\nType=Application\nVersion=1.5\n");
    s.push_str(&format!("Name={name}\nComment={comment}\n"));
    s.push_str(&format!("Exec={}\n", desktop_exec(argv)?));
    s.push_str(&format!("Path={}\n", desktop_string(&workdir.to_string_lossy())));
    s.push_str(&format!("Icon={}\n", desktop_string(icon)));
    s.push_str("Terminal=false\nCategories=Development;Utility;\nStartupNotify=true\n");
    if let Some(c) = wm_class {
        s.push_str(&format!("StartupWMClass={c}\n"));
    }
    Ok(s)
}

/// `<root>/cli/icons/crow-256.png` when it is there, else the theme name `crow`.
pub fn icon_for(install_root: &Path) -> String {
    let png = install_root.join("cli").join("icons").join("crow-256.png");
    if png.is_file() { png.to_string_lossy().into_owned() } else { "crow".into() }
}

/// The operating-point window's entry ("Crow Operating Points").
pub fn boot_entry(python: &Path, install_root: &Path, default_root: Option<&Path>) -> Result<String, String> {
    let script = install_root.join("cli").join("crow_boot.py");
    let (program, args) = window_command(python, &script, install_root, default_root);
    let mut argv = vec![program.into_os_string()];
    argv.extend(args);
    argv.extend(models_flag(install_root));
    desktop_entry(
        "Crow Operating Points",
        "Start a model, then open the Crow window",
        &argv,
        install_root,
        &icon_for(install_root),
        None,
    )
}

/// The Crow window's entry ("Crow"), install.sh's launcher: `<python> <root>/cli/crow_gui.py`.
pub fn crow_entry(python: &Path, install_root: &Path) -> Result<String, String> {
    let argv = vec![python.as_os_str().to_os_string(), install_root.join("cli").join("crow_gui.py").into_os_string()];
    desktop_entry(
        "Crow",
        "Crow's window: a local-LLM agent with tools, memory and a browser pane",
        &argv,
        install_root,
        &icon_for(install_root),
        Some("crow"),
    )
}

/// Write `text` to `path` with `mode`, through a sibling and a rename.
#[cfg(not(windows))]
fn write_entry(path: &Path, text: &str, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let tmp = path.with_extension("desktop.tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("{}: {e}", path.display()))?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode)).map_err(|e| format!("{}: {e}", path.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

/// Linux: the entries into each folder (see the module docs). The launcher
/// folder is created when missing; any other folder must exist (as crow_boot
/// asks on Windows). Entries outside the launcher folder are executable: file
/// managers start only a trusted (executable) entry from the desktop.
#[cfg(not(windows))]
pub fn shortcuts(py: &PythonInfo, install_root: &Path, dirs: &[PathBuf]) -> Result<(), String> {
    boot_script(install_root)?;
    let default = default_install_root();
    let launchers = start_menu_dir();
    let mut failed = Vec::new();
    for dir in shortcut_dirs(dirs, None) {
        let is_launchers = launchers.as_deref().is_some_and(|l| same_path(l, &dir));
        if is_launchers {
            let _ = std::fs::create_dir_all(&dir);
        }
        if !dir.is_dir() {
            failed.push(format!("{}: no such folder", dir.display()));
            continue;
        }
        let mode = if is_launchers { 0o644 } else { 0o755 };
        let mut wanted = vec![(BOOT_SHORTCUT, boot_entry(&py.exe, install_root, default.as_deref()))];
        if is_launchers {
            wanted.push((CROW_SHORTCUT, crow_entry(&py.exe, install_root)));
        }
        for (name, entry) in wanted {
            if let Err(e) = entry.and_then(|text| write_entry(&dir.join(name), &text, mode)) {
                failed.push(format!("{}: {e}", dir.join(name).display()));
            }
        }
    }
    if failed.is_empty() { Ok(()) } else { Err(format!("shortcut not written: {}", failed.join("; "))) }
}

#[cfg(windows)]
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
    #[cfg(not(windows))]
    cmd.stdin(std::process::Stdio::null());
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
