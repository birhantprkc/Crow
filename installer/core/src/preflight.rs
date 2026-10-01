//! T2: what this machine can run (GPU via nvidia-smi, RAM, disk, WebView2, 64-bit).

use crate::api::PreflightReport;
use crate::stack::Stack;
use std::path::Path;

pub fn run(_stack: &Stack, _install_root: &Path) -> PreflightReport {
    unimplemented!("T2")
}
