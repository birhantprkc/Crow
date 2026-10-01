//! #196 P2-E2E fix 1: the installer always installs models into
//! `<install>\models`; `$CROW_MODELS` (the owner's lab root for his llama.cpp
//! lines) must not redirect an install. Its own test binary: it sets the
//! process environment.

use crowsetup_core::run::models_root;
use std::path::Path;

#[test]
fn crow_models_does_not_redirect_the_install() {
    // SAFETY: the only test in this binary; nothing else reads the environment concurrently.
    unsafe { std::env::set_var("CROW_MODELS", r"C:\lab\models") };
    assert_eq!(models_root(Path::new(r"D:\Crow")), Path::new(r"D:\Crow").join("models"));
}
