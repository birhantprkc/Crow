//! T3 finish: the arguments for `crow_boot.py --create-shortcut` and for the
//! operating-point window the boot menu button opens. Nothing here writes a real shortcut or opens a window.

use crowsetup_core::finish;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

fn strs(v: &[OsString]) -> Vec<String> {
    v.iter().map(|s| s.to_string_lossy().into_owned()).collect()
}

#[cfg(windows)]
#[test]
fn paths_compare_like_ntfs() {
    assert!(finish::same_path(Path::new("C:\\Users\\U\\AppData\\Local\\Crow"), Path::new("c:/users/u/appdata/local/crow/")));
    assert!(!finish::same_path(Path::new("C:\\Crow"), Path::new("D:\\Crow")));
}

#[cfg(windows)]
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

#[cfg(windows)]
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
fn the_window_runs_under_pythonw_beside_the_python() {
    let tmp = tempfile::tempdir().unwrap();
    let py = tmp.path().join("python.exe");
    fs::write(&py, b"").unwrap();
    // no pythonw.exe there: the console Python stands in
    assert_eq!(finish::windowed_python(&py), py);
    fs::write(tmp.path().join("pythonw.exe"), b"").unwrap();
    assert_eq!(finish::windowed_python(&py), tmp.path().join("pythonw.exe"));
}

#[test]
fn the_boot_menu_button_opens_the_window_like_the_shortcut() {
    let tmp = tempfile::tempdir().unwrap();
    let py = tmp.path().join("python.exe");
    fs::write(&py, b"").unwrap();
    fs::write(tmp.path().join("pythonw.exe"), b"").unwrap();
    let root = Path::new("D:\\Apps\\Crow");
    let script = root.join("cli").join("crow_boot.py");
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let (prog, args) = finish::window_command(&py, &script, root, Some(default));
    assert_eq!(prog, tmp.path().join("pythonw.exe"));
    assert_eq!(
        strs(&args),
        vec![script.to_string_lossy().into_owned(), "--gui".into(), "--install-root".into(), "D:\\Apps\\Crow".into()]
    );
}

#[test]
fn the_default_root_opens_the_window_without_a_root_flag() {
    let py = Path::new("C:\\nowhere\\python.exe");
    let default = Path::new("C:\\Users\\u\\AppData\\Local\\Crow");
    let script = default.join("cli").join("crow_boot.py");
    let (prog, args) = finish::window_command(py, &script, default, Some(default));
    assert_eq!(prog, py);
    assert_eq!(strs(&args), vec![script.to_string_lossy().into_owned(), "--gui".into()]);
}

/// A stand-in Python: a batch file that appends its arguments to `args.txt`.
#[cfg(windows)]
fn recording_python(dir: &Path) -> (PathBuf, PathBuf) {
    let log = dir.join("args.txt");
    let bat = dir.join("python.bat");
    fs::write(&bat, format!("@echo off\r\necho %*>>\"{}\"\r\n", log.display())).unwrap();
    (bat, log)
}

#[cfg(windows)]
fn fake_install(dir: &Path) -> PathBuf {
    let root = dir.join("Crow Root");
    fs::create_dir_all(root.join("cli")).unwrap();
    fs::write(root.join("cli").join("crow_boot.py"), b"").unwrap();
    root
}

/// #196 P2-E2E fixes 1 and 3: each shortcut is written with `--models
/// <install>\models` (so `$CROW_MODELS` cannot point the boot menu elsewhere),
/// and only into the folders the run asks for -- the Start menu is the run's
/// choice (`RunOptions::start_menu_dir`), not added here behind its back.
#[cfg(windows)]
#[test]
fn shortcuts_carry_the_models_root_and_go_only_where_asked() {
    let tmp = tempfile::tempdir().unwrap();
    let (bat, log) = recording_python(tmp.path());
    let root = fake_install(tmp.path());
    let links = tmp.path().join("links");
    let py = crowsetup_core::python::PythonInfo { exe: bat, ..Default::default() };
    finish::shortcuts(&py, &root, std::slice::from_ref(&links)).unwrap();
    let calls: Vec<String> = fs::read_to_string(&log).unwrap().lines().map(str::to_string).collect();
    assert_eq!(calls.len(), 1, "one shortcut per asked folder, no Start menu added: {calls:?}");
    let models = root.join("models").to_string_lossy().into_owned();
    assert!(calls[0].contains("--models") && calls[0].contains(&models), "{calls:?}");
}

/// #196 P2-E2E fix 1: the boot menu button hands on the models root too.
#[cfg(windows)]
#[test]
fn the_boot_menu_button_carries_the_models_root() {
    let tmp = tempfile::tempdir().unwrap();
    let (bat, log) = recording_python(tmp.path());
    let root = fake_install(tmp.path());
    let py = crowsetup_core::python::PythonInfo { exe: bat, ..Default::default() };
    finish::open_boot_menu(&py, &root).unwrap();
    let end = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while !log.exists() && std::time::Instant::now() < end {
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    let call = fs::read_to_string(&log).unwrap();
    let models = root.join("models").to_string_lossy().into_owned();
    assert!(call.contains("--gui") && call.contains("--models") && call.contains(&models), "{call}");
}

// ------------------------------------------------------------------ Linux (#342)

#[cfg(not(windows))]
#[test]
fn the_xdg_data_home_rules() {
    let x = |a: Option<&str>, h: Option<&str>| finish::xdg_data_home_from(a.map(Into::into), h.map(Into::into));
    assert_eq!(x(Some("/x/data"), Some("/home/u")), Some(PathBuf::from("/x/data")));
    // the spec ignores a relative XDG_DATA_HOME
    assert_eq!(x(Some("rel/data"), Some("/home/u")), Some(PathBuf::from("/home/u/.local/share")));
    assert_eq!(x(None, Some("/home/u")), Some(PathBuf::from("/home/u/.local/share")));
    assert_eq!(x(None, None), None);
    // the default root is install.sh's CROW_HOME, the launchers sit beside it
    let data = finish::xdg_data_home();
    assert_eq!(finish::default_install_root(), data.as_ref().map(|d| d.join("crow")));
    assert_eq!(finish::start_menu_dir(), data.map(|d| d.join("applications")));
    assert!(finish::same_path(Path::new("/a/crow/"), Path::new("/a/crow")));
    assert!(!finish::same_path(Path::new("/a/Crow"), Path::new("/a/crow")), "Linux paths are case-sensitive");
}

#[cfg(not(windows))]
#[test]
fn exec_arguments_are_quoted_and_escaped_like_the_spec_says() {
    assert_eq!(finish::desktop_quote("/opt/crow/venv/bin/python"), "/opt/crow/venv/bin/python");
    assert_eq!(finish::desktop_quote("--gui"), "--gui");
    assert_eq!(finish::desktop_quote("/home/u/My Crow"), "\"/home/u/My Crow\"");
    assert_eq!(finish::desktop_quote("a$b`c\"d"), "\"a\\$b\\`c\\\"d\"");
    assert_eq!(finish::desktop_quote(""), "\"\"");
    let argv: Vec<OsString> = vec!["/p y/python".into(), "a\\b".into(), "100%".into()];
    // one backslash: \\ inside the quotes, doubled again as a string value
    assert_eq!(finish::desktop_exec(&argv).unwrap(), "\"/p y/python\" \"a\\\\\\\\b\" 100%%");
    assert!(finish::desktop_exec(&["a\nb".into()]).is_err());
}

#[cfg(not(windows))]
#[test]
fn the_boot_entry_starts_the_window_with_the_root_and_the_models() {
    let root = Path::new("/srv/My Crow");
    let py = root.join("venv/bin/python");
    let e = finish::boot_entry(&py, root, Some(Path::new("/home/u/.local/share/crow"))).unwrap();
    assert!(e.starts_with("[Desktop Entry]\nType=Application\n"), "{e}");
    assert!(e.contains("Name=Crow Operating Points\n"), "{e}");
    assert!(e.contains(
        "Exec=\"/srv/My Crow/venv/bin/python\" \"/srv/My Crow/cli/crow_boot.py\" --gui \
         --install-root \"/srv/My Crow\" --models \"/srv/My Crow/models\"\n"
    ), "{e}");
    assert!(e.contains("Path=/srv/My Crow\n") && e.contains("Terminal=false\n"), "{e}");
    // the default root carries no --install-root, as on Windows
    let d = Path::new("/home/u/.local/share/crow");
    let e = finish::boot_entry(&d.join("venv/bin/python"), d, Some(d)).unwrap();
    assert!(e.contains("crow_boot.py --gui --models /home/u/.local/share/crow/models\n"), "{e}");
    let c = finish::crow_entry(&d.join("venv/bin/python"), d).unwrap();
    assert!(c.contains("Name=Crow\n") && c.contains("StartupWMClass=crow\n"), "{c}");
    assert!(c.contains("Exec=/home/u/.local/share/crow/venv/bin/python /home/u/.local/share/crow/cli/crow_gui.py\n"), "{c}");
}
