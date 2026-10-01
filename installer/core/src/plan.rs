//! T2: selection -> deduplicated fetch list, smallest first, with disk need.

use crate::api::{Packages, Plan, Selection};
use crate::stack::Stack;
use std::path::Path;

/// `models_root` is `${MODELS}` (`<install>/models` unless overridden).
/// Crow's package and the engine package come first in the plan.
pub fn plan(_stack: &Stack, _sel: &Selection, _models_root: &Path, _packages: &Packages) -> Result<Plan, String> {
    unimplemented!("T2")
}
