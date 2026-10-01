//! T3: unpack Crow's package and the engine package into the install root,
//! with install.ps1's update rules (version from crow_core.py, manifest-directed
//! removal, `.old` for locked files incl. serve.exe).
//!
//! The order is install.ps1's, plus one step in front of it:
//!
//! 1. the package is checked against its own MANIFEST.json INSIDE the zip
//!    (every listed file present with its bytes and sha256, nothing unlisted).
//!    A package that fails is refused before a single byte is written.
//! 2. the version decides (`resolve_action`, install.ps1 `Resolve-InstallAction`
//!    without `-Force`: the installer has no such switch). Equal versions are
//!    "up to date" only when every manifest file on disk still matches;
//!    otherwise the same version is written again (a crash mid-extraction, a
//!    damaged file).
//! 3. the PREVIOUS manifest is read before extraction overwrites it.
//! 4. extraction. A file in `bin\` that cannot be opened for writing (a running
//!    serve.exe, sd-server.exe or llama-server.exe and the DLLs they hold) is
//!    renamed to `<name>.old` and written fresh: Windows locks a running image
//!    against writing, not against renaming (install.ps1 `Move-LockedAside`).
//!    This asks the file rather than the process list, so it covers every
//!    binary and DLL by the same rule. MANIFEST.json is written last, so an
//!    interrupted run still has the old one to compute removals from.
//! 5. every file on disk is verified against the new manifest (`Compare-Manifest`).
//! 6. files the OLD manifest listed and the new one does not are removed
//!    (`Find-DroppedFiles`): a manifest-to-manifest question, never a directory
//!    listing, so `models\`, `session\` and the user's own files cannot be
//!    selected. Counted by looking afterwards.
//! 7. `bin\*.old` is swept, and what stays (still held) is reported, by looking
//!    (`Remove-StaleOld`).
//!
//! The engine zip (crow-nest `tools/pack-engine.ps1`) is flat and lands in
//! `<root>\bin\`, its MANIFEST.json as `bin\MANIFEST.json`. It carries no version
//! inside; the version in the summary comes from the zip's name and "up to date"
//! means every engine file on disk already matches.

use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct ManifestEntry {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Install,
    Update,
    UpToDate,
    Downgrade,
    Unknown,
}

/// Top-level folders of the install root that belong to the installer, not to
/// a package: they never make a root "occupied" and are never removed.
const INSTALLER_OWN: [&str; 4] = ["setup", "models", "python", "session"];

/// MANIFEST.json as the packers write it: a JSON array of {path, bytes, sha256},
/// usually with a UTF-8 BOM; PowerShell writes a one-entry list as an object.
pub fn parse_manifest(bytes: &[u8]) -> Result<Vec<ManifestEntry>, String> {
    let text = std::str::from_utf8(bytes).map_err(|e| format!("MANIFEST.json is not UTF-8: {e}"))?;
    let text = text.trim_start_matches('\u{feff}');
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("MANIFEST.json is not valid JSON: {e}"))?;
    let list = match value {
        serde_json::Value::Array(a) => a,
        obj @ serde_json::Value::Object(_) => vec![obj],
        _ => return Err("MANIFEST.json is neither a list nor an entry".into()),
    };
    list.into_iter()
        .map(|v| serde_json::from_value(v).map_err(|e| format!("MANIFEST.json entry: {e}")))
        .collect()
}

/// `VERSION = "x"` at the start of a line (install.ps1 / pack-release.ps1's pattern).
pub fn version_literal(text: &str) -> Option<String> {
    for line in text.lines() {
        let Some(rest) = line.strip_prefix("VERSION") else { continue };
        let rest = rest.trim_start();
        let Some(rest) = rest.strip_prefix('=') else { continue };
        let rest = rest.trim_start();
        let Some(rest) = rest.strip_prefix('"') else { continue };
        if let Some(end) = rest.find('"')
            && end > 0
        {
            return Some(rest[..end].to_string());
        }
    }
    None
}

/// The installed Crow version: cli\crow_core.py first, cli\crow.py for installs
/// from before #187 (install.ps1 `Get-InstalledVersion`).
pub fn installed_version(root: &Path) -> Option<String> {
    ["crow_core.py", "crow.py"].iter().find_map(|name| {
        let text = fs::read(root.join("cli").join(name)).ok()?;
        version_literal(&String::from_utf8_lossy(&text))
    })
}

fn parse_version(v: &str) -> Option<Vec<u64>> {
    let parts: Vec<&str> = v.split('.').collect();
    if !(2..=4).contains(&parts.len()) {
        return None;
    }
    parts.iter().map(|p| p.parse::<u64>().ok()).collect()
}

/// install.ps1 `Resolve-InstallAction` without `-Force`. `occupied`: the root
/// holds something that is neither the installer's own nor this package's.
pub fn resolve_action(installed: Option<&str>, target: &str, occupied: bool) -> Action {
    let Some(installed) = installed else {
        return if occupied { Action::Unknown } else { Action::Install };
    };
    match (parse_version(installed), parse_version(target)) {
        (Some(a), Some(b)) if b > a => Action::Update,
        (Some(a), Some(b)) if b == a => Action::UpToDate,
        (Some(_), Some(_)) => Action::Downgrade,
        _ => Action::Unknown,
    }
}

fn norm_key(p: &str) -> String {
    p.replace('\\', "/").to_lowercase()
}

/// install.ps1 `Find-DroppedFiles`: paths the previous manifest listed that the
/// current one does not. Case-insensitive, separator-agnostic, `/` in the answer.
pub fn find_dropped(previous: &[String], current: &[String]) -> Vec<String> {
    let now: BTreeSet<String> = current.iter().map(|p| norm_key(p)).collect();
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for p in previous {
        let key = norm_key(p);
        if now.contains(&key) || !seen.insert(key) {
            continue;
        }
        out.push(p.replace('\\', "/"));
    }
    out
}

/// `crow-nest-engine-<version>-win-x64.zip` -> `<version>`.
pub fn engine_version_from_name(zip: &Path) -> Option<String> {
    let name = zip.file_name()?.to_str()?;
    let v = name.strip_prefix("crow-nest-engine-")?.strip_suffix("-win-x64.zip")?;
    (!v.is_empty()).then(|| v.to_string())
}

fn sha256_reader(mut r: impl Read) -> io::Result<(u64, String)> {
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    let mut n = 0u64;
    loop {
        let k = r.read(&mut buf)?;
        if k == 0 {
            break;
        }
        h.update(&buf[..k]);
        n += k as u64;
    }
    Ok((n, hex::encode(h.finalize())))
}

/// Lower-case hex sha256 of a whole file (streamed).
pub fn sha256_file_hex(path: &Path) -> io::Result<String> {
    Ok(sha256_reader(fs::File::open(path)?)?.1)
}

/// A relative path from a zip entry or a manifest, `\` or `/`, refused when it
/// could leave the target (absolute, drive, `..`).
pub(crate) fn safe_rel(name: &str) -> Option<PathBuf> {
    let mut out = PathBuf::new();
    for part in name.split(['/', '\\']) {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." || part.contains(':') {
            return None;
        }
        out.push(part);
    }
    match out.components().next() {
        Some(Component::Normal(_)) => Some(out),
        _ => None,
    }
}

type Archive = zip::ZipArchive<fs::File>;

/// The package, checked against its own manifest before anything is written.
struct Package {
    archive: Archive,
    manifest: Vec<ManifestEntry>,
    manifest_bytes: Vec<u8>,
    /// normalised manifest key -> zip index
    index: BTreeMap<String, usize>,
}

fn open_package(zip_path: &Path) -> Result<Package, String> {
    let file = fs::File::open(zip_path).map_err(|e| format!("cannot open {}: {e}", zip_path.display()))?;
    let mut archive =
        zip::ZipArchive::new(file).map_err(|e| format!("{} is not a readable zip: {e}", zip_path.display()))?;
    let mut entries: BTreeMap<String, usize> = BTreeMap::new();
    let mut manifest_at = None;
    for i in 0..archive.len() {
        let f = archive.by_index(i).map_err(|e| format!("zip entry {i}: {e}"))?;
        if f.is_dir() {
            continue;
        }
        let name = f.name().to_string();
        if safe_rel(&name).is_none() {
            return Err(format!("the package holds an unsafe path: {name}"));
        }
        let key = norm_key(&name);
        if key == "manifest.json" {
            manifest_at = Some(i);
        } else {
            entries.insert(key, i);
        }
    }
    let at = manifest_at.ok_or_else(|| format!("{} has no MANIFEST.json, nothing to verify against", zip_path.display()))?;
    let mut manifest_bytes = Vec::new();
    archive
        .by_index(at)
        .and_then(|mut f| f.read_to_end(&mut manifest_bytes).map_err(Into::into))
        .map_err(|e| format!("cannot read MANIFEST.json: {e}"))?;
    let manifest = parse_manifest(&manifest_bytes)?;

    let mut problems = Vec::new();
    let mut index = BTreeMap::new();
    for e in &manifest {
        let key = norm_key(&e.path);
        if safe_rel(&e.path).is_none() {
            problems.push(format!("unsafe path in MANIFEST.json: {}", e.path));
            continue;
        }
        let Some(&i) = entries.get(&key) else {
            problems.push(format!("missing from the package: {}", e.path));
            continue;
        };
        let f = archive.by_index(i).map_err(|err| format!("{}: {err}", e.path))?;
        match sha256_reader(f) {
            Ok((n, sha)) if n == e.bytes && sha.eq_ignore_ascii_case(&e.sha256) => {}
            Ok(_) => problems.push(format!("does not match MANIFEST.json: {}", e.path)),
            Err(err) => problems.push(format!("unreadable in the package: {} ({err})", e.path)),
        }
        index.insert(key, i);
    }
    for key in entries.keys() {
        if !index.contains_key(key) {
            problems.push(format!("not listed in MANIFEST.json: {key}"));
        }
    }
    if !problems.is_empty() {
        return Err(format!(
            "refusing {}: {}",
            zip_path.file_name().unwrap_or_default().to_string_lossy(),
            problems.join("; ")
        ));
    }
    Ok(Package { archive, manifest, manifest_bytes, index })
}

impl Package {
    fn read_entry(&mut self, path: &str) -> Option<Vec<u8>> {
        let i = *self.index.get(&norm_key(path))?;
        let mut out = Vec::new();
        self.archive.by_index(i).ok()?.read_to_end(&mut out).ok()?;
        Some(out)
    }

    fn paths(&self) -> Vec<String> {
        self.manifest.iter().map(|e| e.path.clone()).collect()
    }
}

/// Missing or mismatched manifest files under `dir` (install.ps1 `Compare-Manifest`).
fn compare_on_disk(dir: &Path, manifest: &[ManifestEntry]) -> Vec<String> {
    let mut bad = Vec::new();
    for e in manifest {
        let Some(rel) = safe_rel(&e.path) else {
            bad.push(format!("unsafe: {}", e.path));
            continue;
        };
        let p = dir.join(rel);
        match fs::metadata(&p) {
            Err(_) => bad.push(format!("missing: {}", e.path)),
            Ok(m) if m.len() != e.bytes => bad.push(format!("corrupt: {}", e.path)),
            Ok(_) => match sha256_file_hex(&p) {
                Ok(s) if s.eq_ignore_ascii_case(&e.sha256) => {}
                _ => bad.push(format!("corrupt: {}", e.path)),
            },
        }
    }
    bad
}

fn read_previous_manifest(dir: &Path, notes: &mut Vec<String>) -> Vec<String> {
    let path = dir.join("MANIFEST.json");
    let Ok(bytes) = fs::read(&path) else { return Vec::new() };
    match parse_manifest(&bytes) {
        Ok(m) => m.into_iter().map(|e| e.path).collect(),
        Err(_) => {
            // not knowing what the last package left means removing nothing
            notes.push("previous MANIFEST.json unreadable, nothing removed".into());
            Vec::new()
        }
    }
}

/// Write one file; in `bin` a file that cannot be opened for writing is renamed
/// to `.old` first (Windows lets a running image be renamed, not written).
fn write_file(dest: &Path, data: &[u8], may_move_aside: bool, moved: &mut Vec<String>) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    }
    let first = fs::File::create(dest);
    let mut file = match first {
        Ok(f) => f,
        Err(e) if may_move_aside && dest.exists() => {
            let mut old = dest.as_os_str().to_os_string();
            old.push(".old");
            let old = PathBuf::from(old);
            // an interrupted update may have left one behind; it is in the way now
            let _ = fs::remove_file(&old);
            fs::rename(dest, &old).map_err(|re| {
                format!(
                    "{} is in use and cannot be moved aside ({e}; {re}); stop the running server and try again",
                    dest.display()
                )
            })?;
            moved.push(dest.file_name().unwrap_or_default().to_string_lossy().into_owned());
            fs::File::create(dest).map_err(|e| format!("cannot write {}: {e}", dest.display()))?
        }
        Err(e) => return Err(format!("cannot write {}: {e}", dest.display())),
    };
    file.write_all(data).map_err(|e| format!("cannot write {}: {e}", dest.display()))
}

/// Delete `bin\*.old`; returns (removed, still held), by looking afterwards.
fn sweep_old(bin: &Path) -> (usize, Vec<String>) {
    let list = |bin: &Path| -> Vec<PathBuf> {
        fs::read_dir(bin)
            .map(|rd| {
                rd.filter_map(|e| e.ok().map(|e| e.path()))
                    .filter(|p| p.is_file() && p.extension().is_some_and(|x| x.eq_ignore_ascii_case("old")))
                    .collect()
            })
            .unwrap_or_default()
    };
    let before = list(bin);
    for p in &before {
        let _ = fs::remove_file(p);
    }
    let kept: Vec<String> = list(bin)
        .iter()
        .map(|p| p.file_name().unwrap_or_default().to_string_lossy().into_owned())
        .collect();
    (before.len() - kept.len(), kept)
}

/// Remove dropped files under `dir`; returns (removed, could not remove).
fn remove_dropped(dir: &Path, dropped: &[String]) -> (usize, Vec<String>) {
    let mut targets = Vec::new();
    for rel in dropped {
        let Some(p) = safe_rel(rel) else { continue };
        // belt and braces: no manifest lists these, and none may ever be cleaned
        let top = p.components().next().map(|c| c.as_os_str().to_string_lossy().to_lowercase());
        if top.is_some_and(|t| INSTALLER_OWN.contains(&t.as_str())) {
            continue;
        }
        let full = dir.join(&p);
        if full.is_file() {
            targets.push((rel.clone(), full));
        }
    }
    for (_, full) in &targets {
        let _ = fs::remove_file(full);
    }
    let stuck: Vec<String> = targets.iter().filter(|(_, f)| f.exists()).map(|(r, _)| r.clone()).collect();
    (targets.len() - stuck.len(), stuck)
}

/// Does `root` hold anything that is neither the installer's own nor a path
/// this package writes? (install.ps1 refuses any non-empty unidentified target;
/// the installer's own `setup\`/`models\` and the files of an interrupted first
/// install of this same package must not count.)
fn occupied(root: &Path, manifest: &[ManifestEntry]) -> bool {
    let ours: BTreeSet<String> = manifest.iter().map(|e| norm_key(&e.path)).collect();
    fn walk(dir: &Path, rel: &str, ours: &BTreeSet<String>) -> bool {
        let Ok(rd) = fs::read_dir(dir) else { return false };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let r = if rel.is_empty() { name.clone() } else { format!("{rel}/{name}") };
            if rel.is_empty() && (INSTALLER_OWN.contains(&name.to_lowercase().as_str()) || name.eq_ignore_ascii_case("MANIFEST.json")) {
                continue;
            }
            let p = e.path();
            if p.is_dir() {
                if walk(&p, &r, ours) {
                    return true;
                }
            } else if !ours.contains(&r.to_lowercase()) {
                return true;
            }
        }
        false
    }
    walk(root, "", &ours)
}

struct Outcome {
    written: usize,
    removed: usize,
    stuck: Vec<String>,
    moved: Vec<String>,
    swept: usize,
    held: Vec<String>,
    notes: Vec<String>,
}

/// Steps 3-7 for a package already checked: write into `dest`, verify, remove
/// dropped files, sweep `bin\*.old`. `in_bin(rel)` says whether a locked file may
/// be moved aside.
fn lay_down(pkg: &mut Package, dest: &Path, bin: &Path, in_bin: &dyn Fn(&Path) -> bool) -> Result<Outcome, String> {
    let mut notes = Vec::new();
    let previous = read_previous_manifest(dest, &mut notes);
    let mut moved = Vec::new();
    let entries = pkg.manifest.clone();
    for e in &entries {
        let rel = safe_rel(&e.path).ok_or_else(|| format!("unsafe path {}", e.path))?;
        let data = pkg.read_entry(&e.path).ok_or_else(|| format!("cannot read {} from the package", e.path))?;
        write_file(&dest.join(&rel), &data, in_bin(&rel), &mut moved)?;
    }
    let mbytes = pkg.manifest_bytes.clone();
    write_file(&dest.join("MANIFEST.json"), &mbytes, false, &mut moved)?;

    let bad = compare_on_disk(dest, &entries);
    if !bad.is_empty() {
        return Err(format!("the files on disk do not match the package: {}", bad.join("; ")));
    }
    let dropped = find_dropped(&previous, &pkg.paths());
    let (removed, stuck) = remove_dropped(dest, &dropped);
    let (swept, held) = sweep_old(bin);
    Ok(Outcome { written: entries.len(), removed, stuck, moved, swept, held, notes })
}

fn describe(head: String, o: &Outcome) -> String {
    let mut s = format!("{head} ({} files", o.written);
    if o.removed > 0 {
        s.push_str(&format!(", {} dropped files removed", o.removed));
    }
    s.push(')');
    if !o.moved.is_empty() {
        s.push_str(&format!("; moved aside as .old while running: {}", o.moved.join(", ")));
    }
    tail(&mut s, o.swept, &o.held, &o.stuck, &o.notes);
    s
}

fn tail(s: &mut String, swept: usize, held: &[String], stuck: &[String], notes: &[String]) {
    if swept > 0 {
        s.push_str(&format!("; {swept} stale .old files removed"));
    }
    if !held.is_empty() {
        s.push_str(&format!("; still held until the server stops: {}", held.join(", ")));
    }
    if !stuck.is_empty() {
        s.push_str(&format!("; could not remove: {}", stuck.join(", ")));
    }
    for n in notes {
        s.push_str("; ");
        s.push_str(n);
    }
}

fn up_to_date(head: String, bin: &Path) -> String {
    let (swept, held) = sweep_old(bin);
    let mut s = head;
    tail(&mut s, swept, &held, &[], &[]);
    s
}

/// Crow's package (`crow-<version>-win-x64.zip`) into `install_root`.
pub fn install_crow_package(zip: &Path, install_root: &Path) -> Result<String, String> {
    let mut pkg = open_package(zip)?;
    let target = ["cli/crow_core.py", "cli/crow.py"]
        .iter()
        .find_map(|p| pkg.read_entry(p).and_then(|b| version_literal(&String::from_utf8_lossy(&b))))
        .ok_or("the package carries no VERSION in cli/crow_core.py")?;
    fs::create_dir_all(install_root).map_err(|e| format!("cannot create {}: {e}", install_root.display()))?;
    let installed = installed_version(install_root);
    let bin = install_root.join("bin");
    let busy = installed.is_none() && occupied(install_root, &pkg.manifest);
    let action = resolve_action(installed.as_deref(), &target, busy);
    let head = match action {
        Action::Unknown => {
            return Err(match &installed {
                Some(v) => format!("cannot compare the installed version {v} with {target}; nothing was changed"),
                None => format!(
                    "{} holds files that do not identify themselves as a Crow install; choose an empty folder",
                    install_root.display()
                ),
            });
        }
        Action::Downgrade => {
            return Err(format!(
                "a newer Crow is installed ({}, this installs {target}); nothing was changed",
                installed.unwrap_or_default()
            ));
        }
        Action::UpToDate => {
            if compare_on_disk(install_root, &pkg.manifest).is_empty() {
                return Ok(up_to_date(format!("Crow {target} is up to date"), &bin));
            }
            format!("Crow {target} repaired")
        }
        Action::Update => format!("Crow {} -> {target} updated", installed.unwrap_or_default()),
        Action::Install => format!("Crow {target} installed"),
    };
    let in_bin = |rel: &Path| {
        rel.components()
            .next()
            .is_some_and(|c| c.as_os_str().eq_ignore_ascii_case("bin"))
    };
    let out = lay_down(&mut pkg, install_root, &bin, &in_bin)?;
    Ok(describe(head, &out))
}

/// The crow-nest engine (`crow-nest-engine-<version>-win-x64.zip`) into `<root>\bin\`.
pub fn install_engine_package(zip: &Path, install_root: &Path) -> Result<String, String> {
    let mut pkg = open_package(zip)?;
    let version = engine_version_from_name(zip).unwrap_or_else(|| "(unversioned)".into());
    let bin = install_root.join("bin");
    fs::create_dir_all(&bin).map_err(|e| format!("cannot create {}: {e}", bin.display()))?;
    let had = bin.join("MANIFEST.json").is_file();
    if had && compare_on_disk(&bin, &pkg.manifest).is_empty() {
        return Ok(up_to_date(format!("engine {version} is up to date"), &bin));
    }
    let head = if had {
        format!("engine updated to {version}")
    } else {
        format!("engine {version} installed")
    };
    let out = lay_down(&mut pkg, &bin, &bin, &|_rel: &Path| true)?;
    Ok(describe(head, &out))
}
