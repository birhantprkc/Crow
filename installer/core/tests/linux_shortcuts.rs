//! #342: the Linux shortcuts are desktop entries written by the installer
//! itself (crow_boot's --create-shortcut writes Windows .lnk files only). Its own
//! test binary: it sets XDG_DATA_HOME and HOME.
#![cfg(unix)]

use crowsetup_core::{finish, python::PythonInfo};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

fn mode(p: &Path) -> u32 {
    fs::metadata(p).unwrap().permissions().mode() & 0o777
}

#[test]
fn the_launcher_folder_gets_both_entries_and_a_chosen_folder_the_boot_entry() {
    let tmp = tempfile::tempdir().unwrap();
    // SAFETY: the only test in this binary; nothing else reads the environment concurrently.
    unsafe {
        std::env::set_var("XDG_DATA_HOME", tmp.path().join("data"));
        std::env::set_var("HOME", tmp.path().join("home"));
    }
    let root = tmp.path().join("Crow Root");
    fs::create_dir_all(root.join("cli/icons")).unwrap();
    fs::write(root.join("cli/crow_boot.py"), b"").unwrap();
    fs::write(root.join("cli/icons/crow-256.png"), b"png").unwrap();
    let desktop = tmp.path().join("home/Desktop");
    fs::create_dir_all(&desktop).unwrap();
    let launchers = finish::start_menu_dir().unwrap();
    assert_eq!(launchers, tmp.path().join("data/applications"));
    assert!(!launchers.exists(), "the test starts without the launcher folder");

    let py = PythonInfo { exe: root.join("venv/bin/python"), ..Default::default() };
    finish::shortcuts(&py, &root, &[desktop.clone(), launchers.clone()]).unwrap();

    let boot = launchers.join(finish::BOOT_SHORTCUT);
    let crow = launchers.join(finish::CROW_SHORTCUT);
    assert_eq!(finish::BOOT_SHORTCUT, "crow-operating-points.desktop");
    assert_eq!((mode(&boot), mode(&crow)), (0o644, 0o644));
    assert_eq!(mode(&desktop.join(finish::BOOT_SHORTCUT)), 0o755, "a desktop entry on the desktop must be trusted");
    assert!(!desktop.join(finish::CROW_SHORTCUT).exists(), "the desktop gets the boot menu only, like Crow.lnk");
    let text = fs::read_to_string(&boot).unwrap();
    let models = root.join("models");
    assert!(text.contains(&format!("--models \"{}\"", models.display())), "{text}");
    assert!(text.contains(&format!("--install-root \"{}\"", root.display())), "{text}");
    assert!(text.contains(&format!("Icon={}", root.join("cli/icons/crow-256.png").display())), "{text}");

    // freedesktop's own validator, where installed
    if let Ok(out) = std::process::Command::new("desktop-file-validate").arg(&boot).arg(&crow).output() {
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stdout));
    }

    // a folder that is not there is named; the others are still written
    let gone = tmp.path().join("nowhere");
    fs::remove_file(&boot).unwrap();
    let err = finish::shortcuts(&py, &root, &[gone, launchers.clone()]).unwrap_err();
    assert!(err.contains("nowhere") && err.contains("no such folder"), "{err}");
    assert!(boot.is_file());
}
