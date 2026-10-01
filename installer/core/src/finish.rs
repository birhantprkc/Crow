//! T3: shortcuts through `crow_boot.py --create-shortcut` and the boot menu launch.

use crate::python::PythonInfo;
use std::path::{Path, PathBuf};

pub fn shortcuts(_py: &PythonInfo, _install_root: &Path, _dirs: &[PathBuf]) -> Result<(), String> {
    unimplemented!("T3")
}

pub fn open_boot_menu(_py: &PythonInfo, _install_root: &Path) -> Result<(), String> {
    unimplemented!("T3")
}
