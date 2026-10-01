//! T3: find Python 3.10+ or unpack the bundled embeddable one; pip packages.

use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct PythonInfo {
    pub exe: PathBuf,
    pub version: String,
    pub bundled: bool,
}

pub fn find() -> Option<PythonInfo> {
    unimplemented!("T3")
}

/// `embedded_zip` / `get_pip` are the bytes the exe carries (None in tests/selftest).
pub fn ensure(
    _install_root: &Path,
    _embedded_zip: Option<&[u8]>,
    _get_pip: Option<&[u8]>,
) -> Result<PythonInfo, String> {
    unimplemented!("T3")
}
