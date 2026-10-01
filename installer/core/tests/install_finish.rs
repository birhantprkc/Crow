//! T3 finish: the arguments for `crow_boot.py --create-shortcut` and for the
//! boot menu launch. Nothing here writes a real shortcut or opens a window.

use crowsetup_core::finish;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

fn strs(v: &[OsString]) -> Vec<String> {
    v.iter().map(|s| s.to_string_lossy().into_owned()).collect()
}

#[test]
fn paths_compare_like_ntfs() {
    assert!(finish::same_path(Path::new("C:\\Users\\U\\AppData\\Local\\Crow"), Path::new("c:/users/u/appdata/local/crow/")));
    assert!(!finish::same_path(Path::new("C:\\Crow"), Path::new("D:\\Crow")));
}

#[test]
fn the_default_root_is_localappdata_crow_and_the_start_menu_is_under_appdata() {
    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from);
    assert_eq!(finish::default_install_root(), local.map(|l| l.join("Crow")));
    let roaming = std::env::var_os("APPDATA").map(PathBuf::from);
    assert_eq!(
        finish::start_menu_dir(),
        roaming.map(|r| r.join("Microsoft").join("Windows").join("Start Menu").join("Programs"))
    );
}

#[test]
fn the_default_root_adds_no_install_root_flag() {
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let script = default.join("cli").join("crow_boot.py");
    let args = finish::shortcut_args(&script, default, Path::new("C:\\Users\\u\\Desktop"), Some(default));
    assert_eq!(
        strs(&args),
        vec![script.to_string_lossy().into_owned(), "--create-shortcut".into(), "C:\\Users\\u\\Desktop".into()]
    );
}

#[test]
fn a_custom_root_is_handed_on() {
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let root = Path::new("D:\\Apps\\Crow");
    let script = root.join("cli").join("crow_boot.py");
    let args = finish::shortcut_args(&script, root, Path::new("D:\\Links"), Some(default));
    assert_eq!(
        strs(&args),
        vec![
            script.to_string_lossy().into_owned(),
            "--create-shortcut".into(),
            "D:\\Links".into(),
            "--install-root".into(),
            "D:\\Apps\\Crow".into(),
        ]
    );
    // without a known default the root is always passed
    let args = finish::shortcut_args(&script, default, Path::new("D:\\Links"), None);
    assert!(strs(&args).contains(&"--install-root".to_string()));
}

#[test]
fn the_start_menu_is_always_added_once() {
    let sm = PathBuf::from("C:\\Users\\u\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs");
    let dirs = finish::shortcut_dirs(&[PathBuf::from("C:\\Users\\u\\Desktop")], Some(sm.clone()));
    assert_eq!(dirs, vec![PathBuf::from("C:\\Users\\u\\Desktop"), sm.clone()]);
    let dirs = finish::shortcut_dirs(&[sm.clone(), PathBuf::from("c:/users/u/appdata/roaming/microsoft/windows/start menu/programs")], Some(sm.clone()));
    assert_eq!(dirs, vec![sm]);
    assert!(finish::shortcut_dirs(&[], None).is_empty());
}

#[test]
fn windows_terminal_is_found_on_path_even_in_windowsapps() {
    let tmp = tempfile::tempdir().unwrap();
    let apps = tmp.path().join("Microsoft").join("WindowsApps");
    fs::create_dir_all(&apps).unwrap();
    fs::write(apps.join("wt.exe"), b"").unwrap();
    let path = std::env::join_paths([tmp.path(), &apps]).unwrap();
    assert_eq!(finish::find_on_path("wt.exe", &path), Some(apps.join("wt.exe")));
    assert_eq!(finish::find_on_path("nope.exe", &path), None);
}

#[test]
fn the_menu_opens_like_the_shortcut_in_windows_terminal() {
    let py = Path::new("C:\\Python312\\python.exe");
    let root = Path::new("D:\\Apps\\Crow");
    let script = root.join("cli").join("crow_boot.py");
    let wt = Path::new("C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\wt.exe");
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let (prog, args) = finish::menu_command(py, &script, root, Some(wt), Some(default));
    assert_eq!(prog, wt);
    assert_eq!(
        strs(&args),
        vec![
            "--title".to_string(),
            "Crow".into(),
            "-d".into(),
            "D:\\Apps\\Crow".into(),
            "C:\\Python312\\python.exe".into(),
            script.to_string_lossy().into_owned(),
            "--install-root".into(),
            "D:\\Apps\\Crow".into(),
        ]
    );
}

#[test]
fn without_windows_terminal_the_console_python_runs_the_menu() {
    let py = Path::new("C:\\Python312\\python.exe");
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let script = default.join("cli").join("crow_boot.py");
    let (prog, args) = finish::menu_command(py, &script, default, None, Some(default));
    assert_eq!(prog, py);
    assert_eq!(strs(&args), vec![script.to_string_lossy().into_owned()]);
}
