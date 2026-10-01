//! T1: sha256 of files and streams.

use std::path::Path;

/// Lower-case hex sha256 of a whole file.
pub fn sha256_file(_path: &Path) -> std::io::Result<String> {
    unimplemented!("T1")
}
