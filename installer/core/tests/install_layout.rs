//! T3 layout: Crow's package and the engine package, with install.ps1's update
//! rules. Every zip here is synthetic and carries the MANIFEST.json shape
//! tools/pack-release.ps1 and crow-nest tools/pack-engine.ps1 write: a UTF-8 BOM,
//! backslash paths, upper-case sha256, MANIFEST.json not listed in itself.

use crowsetup_core::layout::{self, Action};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

fn sha_upper(data: &[u8]) -> String {
    hex::encode_upper(Sha256::digest(data))
}

fn manifest_json(files: &[(&str, &[u8])]) -> Vec<u8> {
    let entries: Vec<serde_json::Value> = files
        .iter()
        .map(|(p, b)| {
            serde_json::json!({"path": p.replace('/', "\\"), "bytes": b.len(), "sha256": sha_upper(b)})
        })
        .collect();
    let mut out = "\u{feff}".as_bytes().to_vec();
    out.extend(serde_json::to_vec_pretty(&entries).unwrap());
    out
}

/// A zip holding `files` (names as given: `/` or `\`) plus `manifest`.
fn write_zip(path: &Path, files: &[(&str, &[u8])], manifest: &[u8]) {
    let f = fs::File::create(path).unwrap();
    let mut z = zip::ZipWriter::new(f);
    let opts = zip::write::SimpleFileOptions::default();
    for (name, data) in files {
        z.start_file(*name, opts).unwrap();
        z.write_all(data).unwrap();
    }
    z.start_file("MANIFEST.json", opts).unwrap();
    z.write_all(manifest).unwrap();
    z.finish().unwrap();
}

fn package(dir: &Path, name: &str, files: &[(&str, &[u8])]) -> PathBuf {
    let path = dir.join(name);
    write_zip(&path, files, &manifest_json(files));
    path
}

fn crow_core(version: &str) -> Vec<u8> {
    format!("\"\"\"crow core\"\"\"\nimport os\nVERSION = \"{version}\"\n").into_bytes()
}

fn read(path: &Path) -> Vec<u8> {
    fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

struct Fixture {
    _tmp: tempfile::TempDir,
    zips: PathBuf,
    root: PathBuf,
}

fn fixture() -> Fixture {
    let tmp = tempfile::tempdir().unwrap();
    let zips = tmp.path().join("zips");
    let root = tmp.path().join("Crow");
    fs::create_dir_all(&zips).unwrap();
    fs::create_dir_all(&root).unwrap();
    Fixture { _tmp: tmp, zips, root }
}

fn v1(zips: &Path) -> PathBuf {
    let core = crow_core("1.0.0");
    package(
        zips,
        "crow-1.0.0-win-x64.zip",
        &[
            ("cli/crow_core.py", &core),
            ("cli/old_helper.py", b"old helper"),
            ("cli/a.py", b"a version one"),
            // pack-release / Python on Linux writes backslash names into the zip
            ("bin\\llama-server.exe", b"MZ llama one"),
            ("bin/ggml.dll", b"MZ ggml one"),
            ("LICENSE", b"MIT"),
        ],
    )
}

fn v2(zips: &Path) -> PathBuf {
    let core = crow_core("2.0.0");
    package(
        zips,
        "crow-2.0.0-win-x64.zip",
        &[
            ("cli/crow_core.py", &core),
            ("cli/a.py", b"a version two"),
            ("cli/new.py", b"new in two"),
            ("bin/llama-server.exe", b"MZ llama two"),
            ("bin/ggml.dll", b"MZ ggml two"),
            ("LICENSE", b"MIT"),
        ],
    )
}

// ------------------------------------------------------------ pure parts ---

#[test]
fn the_manifest_parses_with_a_bom_and_as_a_single_object() {
    let m = layout::parse_manifest(&manifest_json(&[("bin/x.dll", b"abc")])).unwrap();
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].path, "bin\\x.dll");
    assert_eq!(m[0].bytes, 3);
    assert_eq!(m[0].sha256, "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD");
    // PowerShell's ConvertTo-Json writes ONE entry as an object, not an array
    let one = "\u{feff}{\"path\": \"LICENSE\", \"bytes\": 1, \"sha256\": \"AA\"}";
    let m = layout::parse_manifest(one.as_bytes()).unwrap();
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].path, "LICENSE");
    assert!(layout::parse_manifest(b"not json").is_err());
}

#[test]
fn the_version_is_the_literal_at_the_start_of_a_line() {
    assert_eq!(layout::version_literal("x = 1\nVERSION = \"3.0.0\"\n").as_deref(), Some("3.0.0"));
    assert_eq!(layout::version_literal("VERSION=\"2.9.1\"").as_deref(), Some("2.9.1"));
    assert_eq!(layout::version_literal("  VERSION = \"1\"\nOTHER_VERSION = \"9\""), None);
    assert_eq!(layout::version_literal("no version here"), None);
}

#[test]
fn the_installed_version_reads_crow_core_first_then_crow_py() {
    let f = fixture();
    assert_eq!(layout::installed_version(&f.root), None);
    fs::create_dir_all(f.root.join("cli")).unwrap();
    fs::write(f.root.join("cli/crow.py"), "VERSION = \"2.8.0\"\n").unwrap();
    // pre-#187 install: crow_core.py exists but carries no literal -> crow.py answers
    fs::write(f.root.join("cli/crow_core.py"), "import os\n").unwrap();
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("2.8.0"));
    fs::write(f.root.join("cli/crow_core.py"), "VERSION = \"3.0.0\"\n").unwrap();
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("3.0.0"));
}

#[test]
fn resolve_action_covers_every_branch() {
    assert_eq!(layout::resolve_action(None, "1.0.0", false), Action::Install);
    assert_eq!(layout::resolve_action(None, "1.0.0", true), Action::Unknown);
    assert_eq!(layout::resolve_action(Some("1.0.0"), "1.0.1", true), Action::Update);
    assert_eq!(layout::resolve_action(Some("1.9.0"), "1.10.0", true), Action::Update);
    assert_eq!(layout::resolve_action(Some("2.0.0"), "2.0.0", true), Action::UpToDate);
    assert_eq!(layout::resolve_action(Some("2.0.0"), "1.9.9", true), Action::Downgrade);
    assert_eq!(layout::resolve_action(Some("2.0.0-dev"), "2.0.0", true), Action::Unknown);
}

#[test]
fn dropped_files_are_old_minus_new_case_and_separator_blind() {
    let prev: Vec<String> = ["bin\\llama.dll", "cli\\gone.py", "cli\\a.py", "cli\\gone.py"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let now: Vec<String> = ["bin/LLAMA.dll", "cli\\a.py"].iter().map(|s| s.to_string()).collect();
    assert_eq!(layout::find_dropped(&prev, &now), vec!["cli/gone.py".to_string()]);
    assert!(layout::find_dropped(&[], &now).is_empty());
    assert!(layout::find_dropped(&now, &now).is_empty());
}

#[test]
fn the_engine_version_comes_from_the_zip_name() {
    let p = Path::new("x/crow-nest-engine-0.7.2-16-gba499ae-win-x64.zip");
    assert_eq!(layout::engine_version_from_name(p).as_deref(), Some("0.7.2-16-gba499ae"));
    assert_eq!(layout::engine_version_from_name(Path::new("engine.zip")), None);
}

#[test]
fn sha256_of_a_file_is_hex() {
    let f = fixture();
    let p = f.root.join("abc");
    fs::write(&p, b"abc").unwrap();
    assert_eq!(
        layout::sha256_file_hex(&p).unwrap().to_ascii_uppercase(),
        "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
    );
}

// ------------------------------------------------------------ Crow package ---

#[test]
fn a_fresh_install_lands_every_file_and_leaves_models_and_setup_alone() {
    let f = fixture();
    fs::create_dir_all(f.root.join("models/x")).unwrap();
    fs::write(f.root.join("models/x/big.cnq"), b"weights").unwrap();
    fs::create_dir_all(f.root.join("setup")).unwrap();
    fs::write(f.root.join("setup/state.json"), b"{}").unwrap();

    let msg = layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    assert!(msg.contains("1.0.0") && msg.contains("installed"), "{msg}");
    assert_eq!(read(&f.root.join("cli/a.py")), b"a version one");
    assert_eq!(read(&f.root.join("bin/llama-server.exe")), b"MZ llama one");
    assert!(f.root.join("MANIFEST.json").is_file());
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("1.0.0"));
    assert_eq!(read(&f.root.join("models/x/big.cnq")), b"weights");
    assert_eq!(read(&f.root.join("setup/state.json")), b"{}");
}

#[test]
fn an_update_removes_only_what_the_old_manifest_listed_and_the_new_one_drops() {
    let f = fixture();
    layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    // the user's own files: never in any manifest
    fs::create_dir_all(f.root.join("session")).unwrap();
    fs::write(f.root.join("session/slot.bin"), b"kv").unwrap();
    fs::create_dir_all(f.root.join("session-backup")).unwrap();
    fs::write(f.root.join("session-backup/slot.bin"), b"backup").unwrap();
    fs::write(f.root.join("cli/my-notes.txt"), b"mine").unwrap();
    fs::create_dir_all(f.root.join("models")).unwrap();
    fs::write(f.root.join("models/m.gguf"), b"w").unwrap();

    let msg = layout::install_crow_package(&v2(&f.zips), &f.root).unwrap();
    assert!(msg.contains("1.0.0") && msg.contains("2.0.0"), "{msg}");
    assert!(!f.root.join("cli/old_helper.py").exists(), "dropped file stayed");
    assert_eq!(read(&f.root.join("cli/a.py")), b"a version two");
    assert_eq!(read(&f.root.join("cli/new.py")), b"new in two");
    assert_eq!(read(&f.root.join("session/slot.bin")), b"kv");
    assert_eq!(read(&f.root.join("session-backup/slot.bin")), b"backup");
    assert_eq!(read(&f.root.join("cli/my-notes.txt")), b"mine");
    assert_eq!(read(&f.root.join("models/m.gguf")), b"w");
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("2.0.0"));
}

#[test]
fn the_same_version_is_up_to_date_and_a_damaged_one_is_repaired() {
    let f = fixture();
    let zip = v1(&f.zips);
    layout::install_crow_package(&zip, &f.root).unwrap();
    let msg = layout::install_crow_package(&zip, &f.root).unwrap();
    assert!(msg.contains("up to date"), "{msg}");

    fs::write(f.root.join("cli/a.py"), b"tampered").unwrap();
    let msg = layout::install_crow_package(&zip, &f.root).unwrap();
    assert!(!msg.contains("up to date"), "{msg}");
    assert_eq!(read(&f.root.join("cli/a.py")), b"a version one");
}

#[test]
fn a_manifest_mismatch_is_refused_before_anything_is_written() {
    let f = fixture();
    let core = crow_core("1.0.0");
    let good: &[(&str, &[u8])] = &[("cli/crow_core.py", &core), ("cli/a.py", b"right bytes")];
    let mut manifest = manifest_json(good);
    // the manifest promises other bytes for cli/a.py (same length, other content)
    let zip = f.zips.join("bad.zip");
    write_zip(&zip, &[("cli/crow_core.py", &core), ("cli/a.py", b"wrong bytes")], &manifest);
    let err = layout::install_crow_package(&zip, &f.root).unwrap_err();
    assert!(err.contains("cli") && err.contains("a.py"), "{err}");
    assert_eq!(fs::read_dir(&f.root).unwrap().count(), 0, "a refused package wrote files");

    // a file in the zip the manifest does not list is refused too
    manifest = manifest_json(good);
    let zip = f.zips.join("unlisted.zip");
    write_zip(
        &zip,
        &[("cli/crow_core.py", &core), ("cli/a.py", b"right bytes"), ("cli/stray.py", b"x")],
        &manifest,
    );
    let err = layout::install_crow_package(&zip, &f.root).unwrap_err();
    assert!(err.contains("stray.py"), "{err}");
    assert_eq!(fs::read_dir(&f.root).unwrap().count(), 0);

    // and a zip without MANIFEST.json
    let zip = f.zips.join("nomanifest.zip");
    let mut z = zip::ZipWriter::new(fs::File::create(&zip).unwrap());
    z.start_file("cli/crow_core.py", zip::write::SimpleFileOptions::default()).unwrap();
    z.write_all(&core).unwrap();
    z.finish().unwrap();
    let err = layout::install_crow_package(&zip, &f.root).unwrap_err();
    assert!(err.contains("MANIFEST.json"), "{err}");
}

#[test]
fn a_downgrade_and_an_unidentified_folder_are_refused() {
    let f = fixture();
    layout::install_crow_package(&v2(&f.zips), &f.root).unwrap();
    let err = layout::install_crow_package(&v1(&f.zips), &f.root).unwrap_err();
    assert!(err.contains("2.0.0") && err.contains("1.0.0"), "{err}");
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("2.0.0"));

    let g = fixture();
    fs::write(g.root.join("notes.txt"), b"somebody's").unwrap();
    let err = layout::install_crow_package(&v1(&g.zips), &g.root).unwrap_err();
    assert!(err.contains("identif"), "{err}");
    assert_eq!(read(&g.root.join("notes.txt")), b"somebody's");
    assert!(!g.root.join("cli").exists());
}

#[test]
fn an_interrupted_first_install_is_finished_not_refused() {
    // only files the package itself writes are present, no version yet
    let f = fixture();
    fs::create_dir_all(f.root.join("bin")).unwrap();
    fs::write(f.root.join("bin/llama-server.exe"), b"half").unwrap();
    let msg = layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    assert!(msg.contains("installed"), "{msg}");
    assert_eq!(read(&f.root.join("bin/llama-server.exe")), b"MZ llama one");
}

#[cfg(windows)]
fn hold_like_a_running_binary(path: &Path) -> fs::File {
    use std::os::windows::fs::OpenOptionsExt;
    // FILE_SHARE_READ | FILE_SHARE_DELETE: like a mapped image, it may be read and
    // renamed but not opened for writing
    fs::OpenOptions::new().read(true).share_mode(0x1 | 0x4).open(path).unwrap()
}

#[cfg(windows)]
#[test]
fn a_locked_binary_in_bin_is_renamed_to_old_and_swept_once_free() {
    let f = fixture();
    layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    let exe = f.root.join("bin/llama-server.exe");
    let mut holder = hold_like_a_running_binary(&exe);
    assert!(fs::File::create(&exe).is_err(), "the simulation does not lock the file");

    let msg = layout::install_crow_package(&v2(&f.zips), &f.root).unwrap();
    assert_eq!(read(&exe), b"MZ llama two");
    let mut old = Vec::new();
    holder.read_to_end(&mut old).unwrap();
    assert_eq!(old, b"MZ llama one", "the running process lost its file");
    assert!(msg.contains(".old") || msg.contains("aside"), "{msg}");
    drop(holder);

    // the next run sweeps what is no longer held, by looking
    fs::write(f.root.join("bin/ggml.dll.old"), b"stale").unwrap();
    let msg = layout::install_crow_package(&v2(&f.zips), &f.root).unwrap();
    assert!(msg.contains("up to date"), "{msg}");
    assert!(!f.root.join("bin/llama-server.exe.old").exists());
    assert!(!f.root.join("bin/ggml.dll.old").exists());
}

// ------------------------------------------------------------ engine package ---

fn engine(zips: &Path, version: &str, serve: &[u8], with_license: bool) -> PathBuf {
    let mut files: Vec<(&str, &[u8])> = vec![
        ("serve.exe", serve),
        ("nvrtc64_130_0.dll", b"MZ nvrtc"),
        ("nvrtc-builtins64_133.dll", b"MZ builtins"),
    ];
    if with_license {
        files.push(("LICENSE", b"Apache"));
    }
    package(zips, &format!("crow-nest-engine-{version}-win-x64.zip"), &files)
}

#[test]
fn the_engine_installs_into_bin_beside_crows_binaries() {
    let f = fixture();
    layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    let msg = layout::install_engine_package(&engine(&f.zips, "0.7.2", b"MZ serve 1", true), &f.root).unwrap();
    assert!(msg.contains("0.7.2"), "{msg}");
    assert_eq!(read(&f.root.join("bin/serve.exe")), b"MZ serve 1");
    assert_eq!(read(&f.root.join("bin/nvrtc64_130_0.dll")), b"MZ nvrtc");
    assert_eq!(read(&f.root.join("bin/LICENSE")), b"Apache");
    assert!(f.root.join("bin/MANIFEST.json").is_file());
    // Crow's own bin\ files and root files are not the engine's business
    assert_eq!(read(&f.root.join("bin/llama-server.exe")), b"MZ llama one");
    assert_eq!(read(&f.root.join("LICENSE")), b"MIT");

    let again = layout::install_engine_package(&engine(&f.zips, "0.7.2", b"MZ serve 1", true), &f.root).unwrap();
    assert!(again.contains("up to date"), "{again}");
}

#[test]
fn an_engine_update_drops_only_engine_files_and_handles_a_running_serve() {
    let f = fixture();
    layout::install_crow_package(&v1(&f.zips), &f.root).unwrap();
    layout::install_engine_package(&engine(&f.zips, "0.7.2", b"MZ serve 1", true), &f.root).unwrap();
    #[cfg(windows)]
    let holder = hold_like_a_running_binary(&f.root.join("bin/serve.exe"));

    let msg = layout::install_engine_package(&engine(&f.zips, "0.8.0", b"MZ serve 2", false), &f.root).unwrap();
    assert!(msg.contains("0.8.0"), "{msg}");
    assert_eq!(read(&f.root.join("bin/serve.exe")), b"MZ serve 2");
    assert!(!f.root.join("bin/LICENSE").exists(), "the dropped engine file stayed");
    assert_eq!(read(&f.root.join("bin/llama-server.exe")), b"MZ llama one");
    assert!(f.root.join("cli/crow_core.py").is_file());
    #[cfg(windows)]
    drop(holder);
}
