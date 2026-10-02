//! What the exe carries with the `bundle` feature (installer/vendor, written by
//! installer/build.ps1); None without it, and `--packages` supplies the packages.
//! Linux (#342, installer/build.sh): only `vendor/packages-linux-x64.json`; the
//! system Python makes the venv, so no embeddable Python and no get-pip.py.

#[cfg(all(feature = "bundle", windows))]
pub const PYTHON_ZIP: Option<&[u8]> = Some(include_bytes!("../../vendor/python-3.13-embed-amd64.zip"));
#[cfg(all(feature = "bundle", windows))]
pub const GET_PIP: Option<&[u8]> = Some(include_bytes!("../../vendor/get-pip.py"));
#[cfg(all(feature = "bundle", windows))]
pub const PACKAGES_JSON: Option<&str> = Some(include_str!("../../vendor/packages.json"));

#[cfg(all(feature = "bundle", not(windows)))]
pub const PYTHON_ZIP: Option<&[u8]> = None;
#[cfg(all(feature = "bundle", not(windows)))]
pub const GET_PIP: Option<&[u8]> = None;
#[cfg(all(feature = "bundle", not(windows)))]
pub const PACKAGES_JSON: Option<&str> = Some(include_str!("../../vendor/packages-linux-x64.json"));

#[cfg(not(feature = "bundle"))]
pub const PYTHON_ZIP: Option<&[u8]> = None;
#[cfg(not(feature = "bundle"))]
pub const GET_PIP: Option<&[u8]> = None;
#[cfg(not(feature = "bundle"))]
pub const PACKAGES_JSON: Option<&str> = None;
