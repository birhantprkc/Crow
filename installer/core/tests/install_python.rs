//! T3 python: which interpreter is used, and the embeddable `._pth` edit.

use crowsetup_core::python;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Path, PathBuf};

#[test]
fn the_probe_output_is_the_executable_then_the_version() {
    let (exe, v) = python::parse_probe("C:\\Python312\\python.exe\r\n3.12.4\r\n").unwrap();
    assert_eq!(exe, PathBuf::from("C:\\Python312\\python.exe"));
    assert_eq!(v, "3.12.4");
    assert!(python::parse_probe("").is_none());
    assert!(python::parse_probe("only one line\n").is_none());
    assert!(python::parse_probe("C:\\p.exe\nnot-a-version\n").is_none());
}

#[test]
fn only_3_10_and_newer_counts() {
    assert!(python::version_at_least("3.10.0", 3, 10));
    assert!(python::version_at_least("3.13.1", 3, 10));
    assert!(python::version_at_least("4.0.0", 3, 10));
    assert!(!python::version_at_least("3.9.18", 3, 10));
    assert!(!python::version_at_least("2.7.18", 3, 10));
    assert!(!python::version_at_least("x", 3, 10));
}

#[cfg(windows)]
#[test]
fn the_store_stub_folder_is_recognised() {
    assert!(python::is_windows_apps(Path::new(
        "C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe"
    )));
    assert!(python::is_windows_apps(Path::new("C:\\Users\\u\\AppData\\Local\\Microsoft\\windowsapps")));
    assert!(!python::is_windows_apps(Path::new("C:\\Python312\\python.exe")));
}

#[cfg(windows)]
fn fake_dirs() -> (tempfile::TempDir, PathBuf, PathBuf, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let stub = tmp.path().join("Microsoft").join("WindowsApps");
    let real = tmp.path().join("Python312");
    let empty = tmp.path().join("tools");
    for d in [&stub, &real, &empty] {
        fs::create_dir_all(d).unwrap();
    }
    fs::write(stub.join("python.exe"), b"").unwrap();
    fs::write(real.join("python.exe"), b"MZ").unwrap();
    (tmp, stub, real, empty)
}

#[cfg(windows)]
#[test]
fn path_candidates_skip_the_store_stub_and_folders_without_python() {
    let (_tmp, stub, real, empty) = fake_dirs();
    let path = std::env::join_paths([&stub, &empty, &real]).unwrap();
    assert_eq!(python::path_candidates(&path), vec![real.join("python.exe")]);
}

#[cfg(windows)]
#[test]
fn find_asks_the_launcher_first_then_path_and_never_runs_the_stub() {
    let (_tmp, stub, real, _empty) = fake_dirs();
    let path = std::env::join_paths([&stub, &real]).unwrap();
    let real_exe = real.join("python.exe");

    // the launcher has only 3.9 -> PATH's 3.12 is taken
    let mut calls: Vec<(OsString, Vec<String>)> = Vec::new();
    let found = python::find_with(
        &mut |prog: &OsStr, args: &[&str]| {
            calls.push((prog.to_os_string(), args.iter().map(|s| s.to_string()).collect()));
            if prog == OsStr::new("py") {
                Some("C:\\Python39\\python.exe\n3.9.13\n".to_string())
            } else if Path::new(prog) == real_exe {
                Some(format!("{}\n3.12.4\n", real_exe.display()))
            } else {
                None
            }
        },
        &path,
    )
    .unwrap();
    assert_eq!(found.exe, real_exe);
    assert_eq!(found.version, "3.12.4");
    assert!(!found.bundled);
    assert_eq!(calls[0].0, OsString::from("py"));
    assert_eq!(calls[0].1[0], "-3");
    assert!(calls.iter().all(|(p, _)| !python::is_windows_apps(Path::new(p))), "{calls:?}");

    // a launcher with 3.13 wins and PATH is not asked
    let mut asked_path = false;
    let found = python::find_with(
        &mut |prog: &OsStr, _args: &[&str]| {
            if prog == OsStr::new("py") {
                Some("C:\\Py313\\python.exe\n3.13.0\n".to_string())
            } else {
                asked_path = true;
                None
            }
        },
        &path,
    )
    .unwrap();
    assert_eq!(found.exe, PathBuf::from("C:\\Py313\\python.exe"));
    assert!(!asked_path);

    // nothing usable anywhere
    assert!(python::find_with(&mut |_p: &OsStr, _a: &[&str]| None, &path).is_none());
}

const EMBED_PTH: &str = "python313.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n";

#[test]
fn the_pth_edit_enables_site_and_adds_site_packages_once() {
    let out = python::enable_site(EMBED_PTH);
    let lines: Vec<&str> = out.lines().map(|l| l.trim_end_matches('\r')).collect();
    assert!(lines.contains(&"python313.zip"));
    assert!(lines.contains(&"."));
    assert!(lines.contains(&"Lib\\site-packages"), "{out:?}");
    assert!(lines.contains(&"import site"), "{out:?}");
    assert!(!lines.contains(&"#import site"));
    assert!(out.contains("\r\n"), "line endings changed");
    // idempotent
    assert_eq!(python::enable_site(&out), out);
    // a file without the commented line still gets both
    let bare = python::enable_site("python313.zip\n.\n");
    assert!(bare.lines().any(|l| l == "import site"));
    assert!(bare.lines().any(|l| l == "Lib\\site-packages"));
}

#[test]
fn the_pth_file_in_an_unpacked_python_is_found_and_edited() {
    let tmp = tempfile::tempdir().unwrap();
    fs::write(tmp.path().join("python313._pth"), EMBED_PTH).unwrap();
    fs::write(tmp.path().join("python.exe"), b"MZ").unwrap();
    let pth = python::enable_site_in(tmp.path()).unwrap();
    assert_eq!(pth.file_name().unwrap(), "python313._pth");
    let text = fs::read_to_string(&pth).unwrap();
    assert!(text.lines().any(|l| l.trim_end() == "import site"));
    assert!(text.lines().any(|l| l.trim_end() == "Lib\\site-packages"));

    let none = tempfile::tempdir().unwrap();
    assert!(python::enable_site_in(none.path()).is_err());
}

#[test]
fn ensure_without_python_and_without_an_embedded_zip_says_so() {
    // only meaningful where no system Python exists; elsewhere it must use the found one
    let tmp = tempfile::tempdir().unwrap();
    match python::find() {
        Some(found) => {
            assert!(python::version_at_least(&found.version, 3, 10));
            assert!(found.exe.is_file(), "{}", found.exe.display());
        }
        None => {
            let err = python::ensure(tmp.path(), None, None).unwrap_err();
            assert!(err.contains("Python"), "{err}");
        }
    }
}

/// #342: Linux has no `py` launcher and no `python.exe`; `python3` on PATH is
/// asked first, then `python` (install.sh `command -v python3 || command -v python`).
#[cfg(not(windows))]
#[test]
fn linux_asks_python3_then_python_on_path_and_never_py() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let (a, b) = (tmp.path().join("a"), tmp.path().join("b"));
    fs::create_dir_all(&a).unwrap();
    fs::create_dir_all(&b).unwrap();
    for exe in [a.join("python"), b.join("python3")] {
        fs::write(&exe, b"#!/bin/sh\n").unwrap();
        fs::set_permissions(&exe, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let path = std::env::join_paths([&a, &b]).unwrap();
    assert_eq!(python::path_candidates(&path), vec![b.join("python3"), a.join("python")]);

    // python3 is 3.9 -> python 3.12 is taken; `py` is never run
    let mut calls: Vec<OsString> = Vec::new();
    let found = python::find_with(
        &mut |prog: &OsStr, _args: &[&str]| {
            calls.push(prog.to_os_string());
            if Path::new(prog) == b.join("python3") {
                Some(format!("{}\n3.9.18\n", b.join("python3").display()))
            } else if Path::new(prog) == a.join("python") {
                Some(format!("{}\n3.12.4\n", a.join("python").display()))
            } else {
                None
            }
        },
        &path,
    )
    .unwrap();
    assert_eq!(found.exe, a.join("python"));
    assert!(calls.iter().all(|c| c != "py"), "{calls:?}");
}

/// #342: the venv lives under the install root.
#[cfg(not(windows))]
#[test]
fn the_linux_venv_python_is_under_the_root() {
    assert_eq!(python::venv_python(Path::new("/x/crow")), PathBuf::from("/x/crow/venv/bin/python"));
}
