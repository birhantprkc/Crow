//! T3: unpack Crow's package and the engine package into the install root,
//! with install.ps1's update rules (version from crow_core.py, manifest-directed
//! removal, `.old` for locked files incl. serve.exe).

use std::path::Path;

pub fn install_crow_package(_zip: &Path, _install_root: &Path) -> Result<String, String> {
    unimplemented!("T3")
}

pub fn install_engine_package(_zip: &Path, _install_root: &Path) -> Result<String, String> {
    unimplemented!("T3")
}
