//! Known folders: the Desktop (the default shortcut folder, OneDrive-redirected
//! or not) and the Start menu's Programs folder, from the shell itself.

use std::path::PathBuf;

#[cfg(windows)]
fn known(id: &windows_sys::core::GUID) -> Option<PathBuf> {
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::System::Com::CoTaskMemFree;
    use windows_sys::Win32::UI::Shell::{KF_FLAG_DEFAULT, SHGetKnownFolderPath};
    let mut p: windows_sys::core::PWSTR = std::ptr::null_mut();
    // SAFETY: documented call; the returned buffer is freed with CoTaskMemFree
    // whether or not the call succeeded.
    unsafe {
        let hr = SHGetKnownFolderPath(id, KF_FLAG_DEFAULT as u32, std::ptr::null_mut(), &mut p);
        let out = if hr >= 0 && !p.is_null() {
            let len = (0..).take_while(|&i| *p.add(i) != 0).count();
            Some(PathBuf::from(std::ffi::OsString::from_wide(std::slice::from_raw_parts(p, len))))
        } else {
            None
        };
        CoTaskMemFree(p as *const _);
        out
    }
}

pub fn desktop() -> Option<PathBuf> {
    #[cfg(windows)]
    return known(&windows_sys::Win32::UI::Shell::FOLDERID_Desktop);
    #[cfg(not(windows))]
    return std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Desktop"));
}

pub fn start_menu_programs() -> Option<PathBuf> {
    #[cfg(windows)]
    return known(&windows_sys::Win32::UI::Shell::FOLDERID_Programs);
    #[cfg(not(windows))]
    return None;
}

/// A path as the window shows it: `%LOCALAPPDATA%\...` and `Desktop` read
/// better than the full profile path.
pub fn label(path: &std::path::Path) -> String {
    if desktop().is_some_and(|d| d == path) {
        return "Desktop".into();
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA")
        && let Ok(rest) = path.strip_prefix(PathBuf::from(local))
    {
        return format!("%LOCALAPPDATA%\\{}", rest.display());
    }
    path.display().to_string()
}
