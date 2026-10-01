//! What the exe carries with the `bundle` feature (installer/vendor, written by
//! installer/build.ps1); None without it, and `--packages` supplies the packages.

#[cfg(feature = "bundle")]
pub const PYTHON_ZIP: Option<&[u8]> = Some(include_bytes!("../../vendor/python-3.13-embed-amd64.zip"));
#[cfg(feature = "bundle")]
pub const GET_PIP: Option<&[u8]> = Some(include_bytes!("../../vendor/get-pip.py"));
#[cfg(feature = "bundle")]
pub const PACKAGES_JSON: Option<&str> = Some(include_str!("../../vendor/packages.json"));

#[cfg(not(feature = "bundle"))]
pub const PYTHON_ZIP: Option<&[u8]> = None;
#[cfg(not(feature = "bundle"))]
pub const GET_PIP: Option<&[u8]> = None;
#[cfg(not(feature = "bundle"))]
pub const PACKAGES_JSON: Option<&str> = None;
