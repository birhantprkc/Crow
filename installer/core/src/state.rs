//! T1: `setup/state.json`, written atomically; the truth after "X" or a crash.

use crate::api::Selection;
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

impl StateStore {
    pub fn load(_path: &Path) -> std::io::Result<StateStore> {
        unimplemented!("T1")
    }
    pub fn save(&self) -> std::io::Result<()> {
        unimplemented!("T1")
    }
}
