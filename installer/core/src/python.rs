//! T3: find Python 3.10+ or unpack the bundled embeddable one; pip packages.
//!
//! `find()` asks the `py` launcher (`py -3`) first, then every `python.exe` on
//! PATH, and takes the first that reports 3.10 or newer. A PATH entry under a
//! `WindowsApps` folder is never run: there sits the Microsoft Store stub, which
//! opens the Store instead of answering.
//!
//! `ensure()`: a found Python is used as it is; only when `pywebview` is not
//! importable there are the window's packages installed into it (install.ps1
//! does the same). Without one, the embeddable zip the exe carries is unpacked
//! into `<root>\python`, `python3XX._pth` gets `import site` and
//! `Lib\site-packages` (Python docs, "The initialization of the sys.path module
//! search path": with a `._pth` file `site` is imported only when a line says
//! `import site`; each other line is a path, relative to the file), pip comes
//! from `get-pip.py`, then `pywebview faster-whisper sounddevice`. pywebview is
//! required (no window without it): its failure is an `Err`. The two voice
//! packages only warn, in `PythonInfo::warnings` (install.ps1: "typing is
//! unaffected").

use std::ffi::OsStr;
use std::fs;
use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Default)]
pub struct PythonInfo {
    pub exe: PathBuf,
    pub version: String,
    pub bundled: bool,
    /// Things that did not work but do not stop the install (the voice packages).
    pub warnings: Vec<String>,
}

/// Prints `sys.executable`, then `major.minor.micro`.
pub const PROBE_CODE: &str = "import sys; print(sys.executable); print('%d.%d.%d' % sys.version_info[:3])";

const REQUIRED: &[&str] = &["pywebview"];
const VOICE: &[&str] = &["faster-whisper", "sounddevice"];

/// A command that opens no console window (the installer is a GUI app) and
/// reads nothing from stdin.
pub(crate) fn quiet(program: impl AsRef<OsStr>) -> Command {
    let mut c = Command::new(program);
    c.stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        c.creation_flags(CREATE_NO_WINDOW);
    }
    c
}

/// The last lines of a process's output, for an error message.
pub(crate) fn output_tail(out: &std::process::Output, lines: usize) -> String {
    let text = format!("{}\n{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    let rows: Vec<&str> = text.lines().map(str::trim_end).filter(|l| !l.is_empty()).collect();
    rows[rows.len().saturating_sub(lines)..].join(" | ")
}

pub fn parse_probe(out: &str) -> Option<(PathBuf, String)> {
    let mut lines = out.lines().map(str::trim).filter(|l| !l.is_empty());
    let exe = lines.next()?;
    let version = lines.next()?;
    let ok = version.split('.').count() >= 2 && version.split('.').all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
    ok.then(|| (PathBuf::from(exe), version.to_string()))
}

pub fn version_at_least(version: &str, major: u32, minor: u32) -> bool {
    let mut it = version.split('.').map(|p| p.parse::<u32>());
    match (it.next(), it.next()) {
        (Some(Ok(a)), Some(Ok(b))) => (a, b) >= (major, minor),
        _ => false,
    }
}

/// Is any folder of `path` named `WindowsApps` (the Store stub's home)?
pub fn is_windows_apps(path: &Path) -> bool {
    path.components().any(|c| c.as_os_str().eq_ignore_ascii_case("WindowsApps"))
}

/// Every `python.exe` on `path_var`, in order, without the Store stub.
pub fn path_candidates(path_var: &OsStr) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for dir in std::env::split_paths(path_var) {
        if dir.as_os_str().is_empty() || is_windows_apps(&dir) {
            continue;
        }
        let exe = dir.join("python.exe");
        if exe.is_file() && !out.contains(&exe) {
            out.push(exe);
        }
    }
    out
}

/// `find()` with the process runner and PATH handed in. `run(program, args)`
/// returns stdout when the process exited 0.
pub fn find_with(run: &mut dyn FnMut(&OsStr, &[&str]) -> Option<String>, path_var: &OsStr) -> Option<PythonInfo> {
    let accept = |out: Option<String>| -> Option<PythonInfo> {
        let (exe, version) = parse_probe(&out?)?;
        version_at_least(&version, 3, 10).then_some(PythonInfo { exe, version, bundled: false, warnings: vec![] })
    };
    if let Some(found) = accept(run(OsStr::new("py"), &["-3", "-c", PROBE_CODE])) {
        return Some(found);
    }
    for exe in path_candidates(path_var) {
        if let Some(found) = accept(run(exe.as_os_str(), &["-c", PROBE_CODE])) {
            return Some(found);
        }
    }
    None
}

fn run_stdout(program: &OsStr, args: &[&str]) -> Option<String> {
    let out = quiet(program).args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

pub fn find() -> Option<PythonInfo> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    find_with(&mut run_stdout, &path)
}

/// The embeddable `._pth` with `import site` switched on and `Lib\site-packages`
/// listed once. Idempotent; keeps the file's line endings.
pub fn enable_site(pth: &str) -> String {
    let eol = if pth.contains("\r\n") { "\r\n" } else { "\n" };
    let mut lines: Vec<String> = pth.lines().map(|l| l.trim_end_matches('\r').to_string()).collect();
    for l in lines.iter_mut() {
        let t = l.trim();
        if t.starts_with('#') && t.trim_start_matches('#').trim() == "import site" {
            *l = "import site".into();
        }
    }
    let has_sp = lines.iter().any(|l| {
        let t = l.trim().replace('/', "\\");
        t.eq_ignore_ascii_case("Lib\\site-packages")
    });
    let site_at = lines.iter().position(|l| l.trim() == "import site");
    if !has_sp {
        match site_at {
            Some(i) => lines.insert(i, "Lib\\site-packages".into()),
            None => lines.push("Lib\\site-packages".into()),
        }
    }
    if !lines.iter().any(|l| l.trim() == "import site") {
        lines.push("import site".into());
    }
    let mut out = lines.join(eol);
    out.push_str(eol);
    out
}

/// Find `python3XX._pth` in an unpacked embeddable Python and edit it.
pub fn enable_site_in(dir: &Path) -> Result<PathBuf, String> {
    let rd = fs::read_dir(dir).map_err(|e| format!("cannot read {}: {e}", dir.display()))?;
    let pth = rd
        .flatten()
        .map(|e| e.path())
        .find(|p| {
            let n = p.file_name().unwrap_or_default().to_string_lossy().to_lowercase();
            n.starts_with("python") && n.ends_with("._pth")
        })
        .ok_or_else(|| format!("no python*._pth in {}: not an embeddable Python", dir.display()))?;
    let text = fs::read_to_string(&pth).map_err(|e| format!("cannot read {}: {e}", pth.display()))?;
    let new = enable_site(&text);
    if new != text {
        fs::write(&pth, new).map_err(|e| format!("cannot write {}: {e}", pth.display()))?;
    }
    Ok(pth)
}

fn probe(exe: &Path) -> Option<PythonInfo> {
    let (_, version) = parse_probe(&run_stdout(exe.as_os_str(), &["-c", PROBE_CODE])?)?;
    Some(PythonInfo { exe: exe.to_path_buf(), version, bundled: false, warnings: vec![] })
}

fn has_module(py: &Path, module: &str) -> bool {
    let code = format!("import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('{module}') else 1)");
    quiet(py).args(["-c", &code]).output().map(|o| o.status.success()).unwrap_or(false)
}

fn pip_install(py: &Path, packages: &[&str]) -> Result<(), String> {
    let out = quiet(py)
        .args(["-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location", "--quiet"])
        .args(packages)
        .output()
        .map_err(|e| format!("cannot run pip: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    Err(format!(
        "pip install {} failed ({}): {} -- by hand: \"{}\" -m pip install {}",
        packages.join(" "),
        out.status,
        output_tail(&out, 3),
        py.display(),
        packages.join(" ")
    ))
}

/// pywebview (required) and the voice packages (warning only), when pywebview
/// is not importable yet.
fn ensure_packages(py: &mut PythonInfo) -> Result<(), String> {
    if has_module(&py.exe, "webview") {
        return Ok(());
    }
    pip_install(&py.exe, REQUIRED).map_err(|e| format!("the window needs pywebview: {e}"))?;
    if let Err(e) = pip_install(&py.exe, VOICE) {
        py.warnings.push(format!("dictation will not work (typing is unaffected): {e}"));
    }
    Ok(())
}

fn unpack(zip_bytes: &[u8], dest: &Path) -> Result<(), String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(zip_bytes)).map_err(|e| format!("the embedded Python zip: {e}"))?;
    for i in 0..archive.len() {
        let mut f = archive.by_index(i).map_err(|e| format!("the embedded Python zip: {e}"))?;
        if f.is_dir() {
            continue;
        }
        let rel = crate::layout::safe_rel(f.name()).ok_or_else(|| format!("unsafe path in the embedded Python: {}", f.name()))?;
        let out = dest.join(rel);
        if let Some(parent) = out.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
        }
        let mut data = Vec::new();
        f.read_to_end(&mut data).map_err(|e| format!("the embedded Python zip: {e}"))?;
        fs::write(&out, data).map_err(|e| format!("cannot write {}: {e}", out.display()))?;
    }
    Ok(())
}

/// `embedded_zip` / `get_pip` are the bytes the exe carries (None in tests/selftest).
pub fn ensure(install_root: &Path, embedded_zip: Option<&[u8]>, get_pip: Option<&[u8]>) -> Result<PythonInfo, String> {
    if let Some(mut py) = find() {
        ensure_packages(&mut py)?;
        return Ok(py);
    }
    let dir = install_root.join("python");
    let exe = dir.join("python.exe");
    // a previous run may have unpacked it already
    if probe(&exe).is_none() {
        let zip = embedded_zip.ok_or("no Python 3.10 or newer was found and this build carries no embeddable Python")?;
        unpack(zip, &dir)?;
        enable_site_in(&dir)?;
    }
    let mut py = probe(&exe).ok_or_else(|| format!("the bundled Python at {} does not start", exe.display()))?;
    py.bundled = true;
    if !has_module(&exe, "pip") {
        let bytes = get_pip.ok_or("the bundled Python has no pip and this build carries no get-pip.py")?;
        let script = dir.join("get-pip.py");
        fs::write(&script, bytes).map_err(|e| format!("cannot write {}: {e}", script.display()))?;
        let out = quiet(&exe)
            .arg(&script)
            .args(["--no-warn-script-location", "--disable-pip-version-check"])
            .output()
            .map_err(|e| format!("cannot run get-pip.py: {e}"));
        let _ = fs::remove_file(&script);
        let out = out?;
        if !out.status.success() || !has_module(&exe, "pip") {
            return Err(format!("get-pip.py failed ({}): {}", out.status, output_tail(&out, 3)));
        }
    }
    ensure_packages(&mut py)?;
    Ok(py)
}
