//! T3: run tools/te_rename.py for the image stack, then delete text_encoder/.

use crate::python::PythonInfo;
use std::path::Path;

pub fn image_text_encoder(_py: &PythonInfo, _install_root: &Path, _models_root: &Path) -> Result<(), String> {
    unimplemented!("T3")
}
