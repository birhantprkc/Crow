//! T3: the check step. Resolves every selected point exactly like the boot
//! menu (`crow_boot` plan) and verifies files, sizes, sha and that the binaries start.

use crate::python::PythonInfo;
use std::path::Path;

pub fn check_point(_py: &PythonInfo, _install_root: &Path, _models_root: &Path, _point: &str) -> Result<(), String> {
    unimplemented!("T3")
}
