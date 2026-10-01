//! T1: `setup/state.json`, written atomically; the truth after "X" or a crash.

use crate::api::Selection;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct FileState {
    pub etag: Option<String>,
    pub bytes_done: u64,
    pub verified: bool,
}

#[derive(Debug, Default)]
pub struct StateStore {
    pub path: PathBuf,
    pub selection: Option<Selection>,
    pub files: std::collections::BTreeMap<String, FileState>,
    /// Completed install steps by name (`crow`, `engine`, `convert`, ...).
    pub steps_done: Vec<String>,
}

/// The JSON on disk: everything but `path`.
#[derive(serde::Serialize, serde::Deserialize, Default)]
#[serde(default)]
struct OnDisk {
    selection: Option<Selection>,
    files: BTreeMap<String, FileState>,
    steps_done: Vec<String>,
}

#[derive(serde::Serialize)]
struct OnDiskRef<'a> {
    selection: &'a Option<Selection>,
    files: &'a BTreeMap<String, FileState>,
    steps_done: &'a Vec<String>,
}

/// `<path><suffix>` in the same directory.
fn sibling(path: &Path, suffix: &str) -> PathBuf {
    let mut s: OsString = path.as_os_str().to_owned();
    s.push(suffix);
    s.into()
}

impl StateStore {
    /// A missing file is an empty store; a corrupt one is copied to
    /// `<path>.bad` and the store starts empty.
    pub fn load(path: &Path) -> std::io::Result<StateStore> {
        let empty = StateStore {
            path: path.to_path_buf(),
            ..Default::default()
        };
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(empty),
            Err(e) => return Err(e),
        };
        match serde_json::from_slice::<OnDisk>(&bytes) {
            Ok(d) => Ok(StateStore {
                path: path.to_path_buf(),
                selection: d.selection,
                files: d.files,
                steps_done: d.steps_done,
            }),
            Err(_) => {
                std::fs::copy(path, sibling(path, ".bad"))?;
                Ok(empty)
            }
        }
    }

    /// Write `<path>.tmp` in the same directory, flush it to disk, then rename
    /// it over `path` (replaces atomically on NTFS and POSIX).
    pub fn save(&self) -> std::io::Result<()> {
        if let Some(dir) = self.path.parent().filter(|d| !d.as_os_str().is_empty()) {
            std::fs::create_dir_all(dir)?;
        }
        let json = serde_json::to_vec_pretty(&OnDiskRef {
            selection: &self.selection,
            files: &self.files,
            steps_done: &self.steps_done,
        })?;
        let tmp = sibling(&self.path, ".tmp");
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(&json)?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, &self.path)
    }

    /// The entry for file `id`, created empty when missing.
    pub fn file_mut(&mut self, id: &str) -> &mut FileState {
        self.files.entry(id.to_string()).or_default()
    }
}
