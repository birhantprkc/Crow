//! #340: a point's video server runtime -- ComfyUI's portable archive as
//! ComfyUI publishes it, unpacked into its folder.
//!
//! THE ORIGINAL, NOT A REPACK (the owner, 2026-10-03: nothing foreign is
//! mirrored or repacked into our repos). ComfyUI builds it with `7z a -t7z
//! -m0=lzma2 -mx=9 -mfb=128 -md=768m -ms=on -mf=BCJ2` (its
//! .github/workflows/stable-release.yml at v0.38.0): one solid LZMA2 stream
//! with a BCJ2 filter, read here with sevenz-rust2 on ONE thread. Measured on
//! its 1,994,326,521 B archive (58,293 files, 4,384,588,275 B): 806 MB peak
//! and 40.9 s on one thread, 10.2 GB and 45.7 s on the default 24.
//!
//! INTO `<dir>.part`, THEN SWAPPED: the old runtime is moved to `<dir>.old`,
//! the new one takes its place, the old one is deleted. A failed, refused or
//! cancelled unpack leaves the runtime that was there untouched. Every entry
//! must sit under the archive's top folder (`strip`), which is removed, and
//! may not climb out (`layout::safe_rel`). The marker `<dir>/.crow-runtime`
//! holds the archive's sha256: the same archive again is "up to date".
//!
//! THE MODEL PATHS: ComfyUI loads `ComfyUI/extra_model_paths.yaml` beside its
//! main.py (main.py @ v0.38.0) and adds `base_path` + each folder to that
//! folder kind (utils/extra_config.py). The LTX files live under `${MODELS}`,
//! not inside the runtime, so the file is written on every run.

use crate::api::Selection;
use crate::layout::safe_rel;
use crate::stack::{Stack, resolve_path};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// One runtime to unpack, resolved from stack.json for this platform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeJob {
    pub point: String,
    /// The stack.json file id of the archive.
    pub file_id: String,
    /// The downloaded archive.
    pub archive: PathBuf,
    /// Its sha256, lower-case hex: the marker's content.
    pub sha256: String,
    pub dir: PathBuf,
    /// The archive's top folder, removed from every entry.
    pub strip: String,
    pub model_paths: Option<ModelPaths>,
    /// The unpacked size (stack.json `bytes`): disk the run needs beside the archive.
    pub bytes: u64,
}

/// ComfyUI's extra model search paths.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelPaths {
    /// Relative to the runtime dir: `ComfyUI/extra_model_paths.yaml`.
    pub file: String,
    pub base: PathBuf,
    pub folders: Vec<String>,
}

pub const MARKER: &str = ".crow-runtime";

/// The runtime in `job.dir` was unpacked from this very archive.
pub fn is_current(job: &RuntimeJob) -> bool {
    fs::read_to_string(job.dir.join(MARKER)).is_ok_and(|s| s.trim() == job.sha256)
}

/// The YAML ComfyUI reads. Single-quoted scalars: a Windows path's `\` stays
/// as written, a `'` is doubled.
pub fn model_paths_yaml(mp: &ModelPaths) -> String {
    let q = |s: &str| format!("'{}'", s.replace('\'', "''"));
    let mut y = String::from("# Written by CrowSetup (#340): where ComfyUI finds the models Crow installs.\ncrow:\n");
    y.push_str(&format!("    base_path: {}\n", q(&mp.base.display().to_string())));
    for f in &mp.folders {
        y.push_str(&format!("    {f}: {f}\n"));
    }
    y
}

fn sibling(dir: &Path, suffix: &str) -> PathBuf {
    let mut name = dir.file_name().unwrap_or_default().to_os_string();
    name.push(suffix);
    dir.with_file_name(name)
}

fn remove_dir(p: &Path) -> Result<(), String> {
    match fs::remove_dir_all(p) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(format!("cannot remove {}: {e}", p.display())),
        _ => Ok(()),
    }
}

fn write_model_paths(job: &RuntimeJob) -> Result<(), String> {
    let Some(mp) = &job.model_paths else { return Ok(()) };
    let rel = safe_rel(&mp.file).ok_or_else(|| format!("model paths file {:?} is not a relative path", mp.file))?;
    let path = job.dir.join(rel);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    }
    fs::write(&path, model_paths_yaml(mp)).map_err(|e| format!("cannot write {}: {e}", path.display()))
}

/// Unpack `job.archive` into `job.dir`. `progress(bytes, files)` after each file.
/// Returns the one-line summary.
pub fn unpack(job: &RuntimeJob, cancel: &AtomicBool, progress: &mut dyn FnMut(u64, u64)) -> Result<String, String> {
    if is_current(job) {
        write_model_paths(job)?;
        return Ok("ComfyUI runtime up to date".into());
    }
    let part = sibling(&job.dir, ".part");
    remove_dir(&part)?;
    fs::create_dir_all(&part).map_err(|e| format!("cannot create {}: {e}", part.display()))?;
    let result = extract(job, &part, cancel, progress);
    let (files, bytes) = match result {
        Ok(n) => n,
        Err(e) => {
            let _ = fs::remove_dir_all(&part);
            return Err(e);
        }
    };
    let old = sibling(&job.dir, ".old");
    remove_dir(&old)?;
    if job.dir.exists() {
        fs::rename(&job.dir, &old).map_err(|e| format!("cannot move the old runtime aside ({}): {e}", job.dir.display()))?;
    }
    if let Err(e) = fs::rename(&part, &job.dir) {
        let _ = fs::rename(&old, &job.dir);
        return Err(format!("cannot put the runtime in place ({}): {e}", job.dir.display()));
    }
    remove_dir(&old)?;
    write_model_paths(job)?;
    fs::write(job.dir.join(MARKER), format!("{}\n", job.sha256))
        .map_err(|e| format!("cannot write {}: {e}", job.dir.join(MARKER).display()))?;
    Ok(format!("ComfyUI runtime unpacked ({files} files, {:.1} GB)", bytes as f64 / 1e9))
}

fn extract(job: &RuntimeJob, part: &Path, cancel: &AtomicBool, progress: &mut dyn FnMut(u64, u64)) -> Result<(u64, u64), String> {
    let mut reader = sevenz_rust2::ArchiveReader::open(&job.archive, sevenz_rust2::Password::empty())
        .map_err(|e| format!("{} is not a readable 7z: {e}", job.archive.display()))?;
    reader.set_thread_count(1);
    let prefix = format!("{}/", job.strip);
    let (mut files, mut bytes) = (0u64, 0u64);
    let mut refused: Option<String> = None;
    let mut buf = vec![0u8; 1 << 20];
    let other = |s: String| sevenz_rust2::Error::Other(s.into());
    let walked = reader.for_each_entries(|entry, data| {
        if cancel.load(Ordering::SeqCst) {
            return Err(other("cancelled".into()));
        }
        let name = entry.name.replace('\\', "/");
        if name == job.strip && entry.is_directory {
            return Ok(true);
        }
        let Some(rel) = name.strip_prefix(&prefix).and_then(safe_rel) else {
            refused = Some(format!("{} holds {name:?}, which is not inside {}/", job.archive.display(), job.strip));
            return Err(other("refused".into()));
        };
        let dest = part.join(rel);
        if entry.is_directory {
            fs::create_dir_all(&dest)?;
            return Ok(true);
        }
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut out = std::io::BufWriter::new(fs::File::create(&dest)?);
        loop {
            if cancel.load(Ordering::SeqCst) {
                return Err(other("cancelled".into()));
            }
            let n = data.read(&mut buf)?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n])?;
            bytes += n as u64;
        }
        out.flush()?;
        files += 1;
        progress(bytes, files);
        Ok(true)
    });
    match walked {
        Ok(()) => Ok((files, bytes)),
        Err(_) if refused.is_some() => Err(refused.unwrap_or_default()),
        Err(_) if cancel.load(Ordering::SeqCst) => Err("cancelled".into()),
        Err(e) => Err(format!("unpacking {} failed: {e}", job.archive.display())),
    }
}

/// The runtimes the selected points need on this platform, from stack.json's
/// `video_server.runtime.<platform>`; one per archive, for the first point that
/// names it. A runtime stack.json cannot resolve is left out (check_stack
/// refuses such a stack before it ships).
pub fn jobs(stack: &Stack, sel: &Selection, models: &Path) -> Vec<RuntimeJob> {
    let plat = if cfg!(windows) { "windows" } else { "linux" };
    let install = sel.install_root.as_path();
    let mut out: Vec<RuntimeJob> = Vec::new();
    for p in stack.raw["points"].as_array().into_iter().flatten() {
        let Some(id) = p["id"].as_str().filter(|id| sel.points.iter().any(|x| x == id)) else { continue };
        let rt = &p["video_server"]["runtime"][plat];
        let Some(fid) = rt["file"].as_str() else { continue };
        if out.iter().any(|j| j.file_id == fid) {
            continue;
        }
        let resolved = (|| -> Result<RuntimeJob, String> {
            let f = stack.file(fid).ok_or_else(|| format!("runtime file {fid} is not declared"))?;
            let mp = &rt["model_paths"];
            let model_paths = if mp.is_object() {
                Some(ModelPaths {
                    file: mp["file"].as_str().unwrap_or_default().to_string(),
                    base: resolve_path(mp["base"].as_str().unwrap_or_default(), install, models)?,
                    folders: mp["folders"].as_array().into_iter().flatten()
                        .filter_map(|v| v.as_str().map(str::to_string)).collect(),
                })
            } else {
                None
            };
            Ok(RuntimeJob {
                point: id.to_string(),
                file_id: fid.to_string(),
                archive: resolve_path(&f.dest, install, models)?,
                sha256: f.sha256.to_ascii_lowercase(),
                dir: resolve_path(rt["dir"].as_str().unwrap_or_default(), install, models)?,
                strip: rt["strip"].as_str().unwrap_or_default().to_string(),
                model_paths,
                bytes: rt["bytes"].as_u64().unwrap_or(0),
            })
        })();
        match resolved {
            Ok(j) => out.push(j),
            Err(e) => eprintln!("crowsetup: point {id}: {e}"),
        }
    }
    out
}
