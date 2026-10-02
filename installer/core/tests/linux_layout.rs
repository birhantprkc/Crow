//! #342 layout on Linux: the `.tar.gz` packages (crow-nest's engine pack and
//! Crow's `crow-<v>-linux-x64.tar.gz`), in the shape their packers write: `./`
//! names, an object MANIFEST.json with `glibc_min` and upper-case sha256, the
//! executable bit on serve and sd-server.
#![cfg(unix)]

use crowsetup_core::layout;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

fn sha_upper(data: &[u8]) -> String {
    hex::encode_upper(Sha256::digest(data))
}

/// crow-nest's engine MANIFEST.json: `{"glibc_min", "files": [{path, bytes, sha256}]}`.
fn object_manifest(files: &[(&str, &[u8], u32)], glibc_min: &str) -> Vec<u8> {
    let entries: Vec<serde_json::Value> = files
        .iter()
        .map(|(p, b, _)| serde_json::json!({"path": p.trim_start_matches("./"), "bytes": b.len(), "sha256": sha_upper(b)}))
        .collect();
    serde_json::to_vec_pretty(&serde_json::json!({"glibc_min": glibc_min, "files": entries})).unwrap()
}

/// Crow's MANIFEST.json (tools/pack-release.sh): a bare array, forward slashes.
fn array_manifest(files: &[(&str, &[u8], u32)]) -> Vec<u8> {
    let entries: Vec<serde_json::Value> = files
        .iter()
        .map(|(p, b, _)| serde_json::json!({"path": p, "bytes": b.len(), "sha256": sha_upper(b)}))
        .collect();
    serde_json::to_vec_pretty(&entries).unwrap()
}

/// A regular entry whose name is written byte for byte (tar's own path
/// setter refuses `..` and absolute names, which the refusal tests need).
fn raw_file(b: &mut tar::Builder<flate2::write::GzEncoder<fs::File>>, name: &str, data: &[u8], mode: u32) {
    let mut h = tar::Header::new_ustar();
    h.set_entry_type(tar::EntryType::Regular);
    h.set_size(data.len() as u64);
    h.set_mode(mode);
    let field = &mut h.as_old_mut().name;
    field.fill(0);
    field[..name.len()].copy_from_slice(name.as_bytes());
    h.set_cksum();
    b.append(&h, data).unwrap();
}

fn tar_gz(path: &Path, files: &[(&str, &[u8], u32)], manifest: Option<&[u8]>, extra: impl FnOnce(&mut tar::Builder<flate2::write::GzEncoder<fs::File>>)) {
    let f = fs::File::create(path).unwrap();
    let mut b = tar::Builder::new(flate2::write::GzEncoder::new(f, flate2::Compression::fast()));
    let mut dir = tar::Header::new_ustar();
    dir.set_entry_type(tar::EntryType::Directory);
    dir.set_mode(0o755);
    dir.set_size(0);
    b.append_data(&mut dir, "./", std::io::empty()).unwrap();
    for (name, data, mode) in files {
        raw_file(&mut b, name, data, *mode);
    }
    if let Some(m) = manifest {
        raw_file(&mut b, "./MANIFEST.json", m, 0o644);
    }
    extra(&mut b);
    b.into_inner().unwrap().finish().unwrap();
}

fn read(path: &Path) -> Vec<u8> {
    fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

struct Fx {
    _tmp: tempfile::TempDir,
    packs: PathBuf,
    root: PathBuf,
}

fn fx() -> Fx {
    let tmp = tempfile::tempdir().unwrap();
    let packs = tmp.path().join("packs");
    let root = tmp.path().join("crow");
    fs::create_dir_all(&packs).unwrap();
    fs::create_dir_all(&root).unwrap();
    Fx { _tmp: tmp, packs, root }
}

const ENGINE: [(&str, &[u8], u32); 4] = [
    ("./serve", b"\x7fELF serve 1", 0o755),
    ("./libnvrtc.so", b"\x7fELF nvrtc", 0o755),
    ("./libnvrtc-builtins.so.13.3", b"\x7fELF builtins", 0o755),
    ("./LICENSE", b"Apache-2.0", 0o644),
];

fn engine_pack(dir: &Path, version: &str, files: &[(&str, &[u8], u32)], glibc_min: &str) -> PathBuf {
    let p = dir.join(format!("crow-nest-engine-{version}-linux-x64.tar.gz"));
    tar_gz(&p, files, Some(&object_manifest(files, glibc_min)), |_| {});
    p
}

#[test]
fn the_engine_tarball_lands_in_bin_with_its_modes() {
    let f = fx();
    let pack = engine_pack(&f.packs, "0.8.0", &ENGINE, "2.17");
    let msg = layout::install_engine_package(&pack, &f.root).unwrap();
    assert!(msg.contains("engine 0.8.0 installed") && msg.contains("4 files"), "{msg}");
    assert_eq!(read(&f.root.join("bin/serve")), b"\x7fELF serve 1");
    assert_eq!(read(&f.root.join("bin/libnvrtc.so")), b"\x7fELF nvrtc");
    assert_eq!(read(&f.root.join("bin/LICENSE")), b"Apache-2.0");
    assert_eq!(mode(&f.root.join("bin/serve")), 0o755, "serve must stay executable");
    assert_eq!(mode(&f.root.join("bin/LICENSE")), 0o644);
    assert!(f.root.join("bin/MANIFEST.json").is_file());
    assert!(!f.root.join("bin/serve.crowsetup-new").exists());

    let again = layout::install_engine_package(&pack, &f.root).unwrap();
    assert!(again.contains("up to date"), "{again}");
}

#[test]
fn an_engine_update_replaces_a_running_serve_without_touching_its_inode() {
    let f = fx();
    layout::install_engine_package(&engine_pack(&f.packs, "0.8.0", &ENGINE, "2.17"), &f.root).unwrap();
    let serve = f.root.join("bin/serve");
    // a running serve holds its inode open; Linux keeps it while the name moves on
    let mut held = fs::File::open(&serve).unwrap();

    let mut v2 = ENGINE.to_vec();
    v2[0] = ("./serve", b"\x7fELF serve 2", 0o755);
    v2.pop(); // LICENSE dropped by the new manifest
    let msg = layout::install_engine_package(&engine_pack(&f.packs, "0.8.1", &v2, "2.17"), &f.root).unwrap();
    assert!(msg.contains("0.8.1") && msg.contains("1 dropped files removed"), "{msg}");
    assert_eq!(read(&serve), b"\x7fELF serve 2");
    let mut old = Vec::new();
    held.read_to_end(&mut old).unwrap();
    assert_eq!(old, b"\x7fELF serve 1", "the running serve's file was written over");
    assert!(!f.root.join("bin/serve.old").exists(), "nothing is moved aside on Linux");
    assert!(!f.root.join("bin/LICENSE").exists());
}

#[test]
fn a_newer_glibc_than_the_host_has_refuses_the_engine() {
    assert!(layout::glibc_refusal(Some("2.34"), Some("2.39")).is_none());
    assert!(layout::glibc_refusal(Some("2.34"), Some("2.34")).is_none());
    assert!(layout::glibc_refusal(Some("2.34"), Some("2.31")).unwrap().contains("glibc 2.34"));
    assert!(layout::glibc_refusal(None, Some("2.31")).is_none());
    assert!(layout::glibc_refusal(Some("2.34"), None).is_none(), "off glibc nothing is known");
    assert!(layout::version_at_least("2.40", "2.4"));
    assert!(!layout::version_at_least("2.9", "2.34"));

    let f = fx();
    let err = layout::install_engine_package(&engine_pack(&f.packs, "0.8.0", &ENGINE, "99.0"), &f.root).unwrap_err();
    assert!(err.contains("glibc 99.0"), "{err}");
    assert!(!f.root.join("bin/serve").exists(), "a refused engine writes nothing");
}

#[test]
fn the_engine_version_comes_from_either_name() {
    let v = |n: &str| layout::engine_version_from_name(Path::new(n));
    assert_eq!(v("crow-nest-engine-0.8.0-linux-x64.tar.gz").as_deref(), Some("0.8.0"));
    assert_eq!(v("crow-nest-engine-0.8.0-3-gabc1234-linux-x64.tar.gz").as_deref(), Some("0.8.0-3-gabc1234"));
    assert_eq!(v("crow-nest-engine-0.7.2-win-x64.zip").as_deref(), Some("0.7.2"));
    assert_eq!(v("crow-nest-engine--linux-x64.tar.gz"), None);
}

#[test]
fn the_object_manifest_and_the_bare_array_both_parse() {
    let obj = object_manifest(&ENGINE, "2.34");
    let m = layout::parse_manifest(&obj).unwrap();
    assert_eq!(m.len(), 4);
    assert_eq!(m[0].path, "serve");
    assert_eq!(layout::manifest_glibc_min(&obj).as_deref(), Some("2.34"));
    let arr = array_manifest(&[("bin/sd-server", b"x", 0o755)]);
    assert_eq!(layout::parse_manifest(&arr).unwrap()[0].path, "bin/sd-server");
    assert_eq!(layout::manifest_glibc_min(&arr), None);
    assert!(layout::parse_manifest(br#"{"files": 3}"#).is_err());
}

fn refused(f: &Fx, name: &str, extra: impl FnOnce(&mut tar::Builder<flate2::write::GzEncoder<fs::File>>), files: &[(&str, &[u8], u32)]) -> String {
    let p = f.packs.join(name);
    let man: Vec<(&str, &[u8], u32)> = files.to_vec();
    tar_gz(&p, files, Some(&object_manifest(&man, "2.17")), extra);
    let err = layout::install_engine_package(&p, &f.root).unwrap_err();
    assert!(!f.root.join("bin/serve").exists(), "{name}: something was written before the refusal");
    err
}

#[test]
fn links_absolute_names_dotdot_and_strays_refuse_the_package() {
    let f = fx();
    let err = refused(&f, "crow-nest-engine-1-linux-x64.tar.gz", |b| {
        let mut h = tar::Header::new_gnu();
        h.set_entry_type(tar::EntryType::Symlink);
        h.set_size(0);
        b.append_link(&mut h, "./libcuda.so.1", "/usr/lib/libcuda.so.1").unwrap();
    }, &ENGINE);
    assert!(err.contains("link") && err.contains("libcuda.so.1"), "{err}");

    let err = refused(&f, "crow-nest-engine-2-linux-x64.tar.gz", |b| raw_file(b, "/etc/evil", b"x", 0o644), &ENGINE);
    assert!(err.contains("absolute"), "{err}");

    let err = refused(&f, "crow-nest-engine-3-linux-x64.tar.gz", |b| raw_file(b, "./../evil", b"x", 0o644), &ENGINE);
    assert!(err.contains("unsafe"), "{err}");

    let err = refused(&f, "crow-nest-engine-4-linux-x64.tar.gz", |b| raw_file(b, "./stray.so", b"x", 0o755), &ENGINE);
    assert!(err.contains("not listed in MANIFEST.json: stray.so"), "{err}");

    // a byte changed after the manifest was written
    let p = f.packs.join("crow-nest-engine-5-linux-x64.tar.gz");
    let mut bad = ENGINE.to_vec();
    let man = object_manifest(&bad, "2.17");
    bad[0] = ("./serve", b"\x7fELF serve X", 0o755);
    tar_gz(&p, &bad, Some(&man), |_| {});
    let err = layout::install_engine_package(&p, &f.root).unwrap_err();
    assert!(err.contains("does not match MANIFEST.json: serve"), "{err}");

    let p = f.packs.join("crow-nest-engine-6-linux-x64.tar.gz");
    tar_gz(&p, &ENGINE, None, |_| {});
    assert!(layout::install_engine_package(&p, &f.root).unwrap_err().contains("no MANIFEST.json"));
    assert!(!f.root.join("bin/serve").exists());
}

fn crow_core(version: &str) -> Vec<u8> {
    format!("\"\"\"crow core\"\"\"\nVERSION = \"{version}\"\n").into_bytes()
}

#[test]
fn crows_linux_tarball_installs_with_sd_server_and_the_cuda_runtime() {
    let f = fx();
    let core = crow_core("3.1.0");
    let files: Vec<(&str, &[u8], u32)> = vec![
        ("bin/sd-server", b"\x7fELF sd", 0o755),
        ("cli/crow_core.py", &core, 0o644),
        ("cli/crow_boot.py", b"print('boot')", 0o644),
        ("cuda/lib/libcudart.so.13", b"\x7fELF cudart", 0o755),
        ("cuda/lib/libcublas.so.13", b"\x7fELF cublas", 0o755),
        ("cuda/lib/libcublasLt.so.13", b"\x7fELF cublasLt", 0o755),
        ("manifests/stack.json", b"{}", 0o644),
        ("LICENSE", b"MIT", 0o644),
    ];
    let p = f.packs.join("crow-3.1.0-linux-x64.tar.gz");
    tar_gz(&p, &files, Some(&array_manifest(&files)), |_| {});
    // the installer's own folders do not make the root "occupied"
    fs::create_dir_all(f.root.join("venv/bin")).unwrap();
    fs::create_dir_all(f.root.join("setup/downloads")).unwrap();
    let msg = layout::install_crow_package(&p, &f.root).unwrap();
    assert!(msg.contains("Crow 3.1.0 installed") && msg.contains("8 files"), "{msg}");
    assert_eq!(mode(&f.root.join("bin/sd-server")), 0o755);
    assert_eq!(read(&f.root.join("cuda/lib/libcublasLt.so.13")), b"\x7fELF cublasLt");
    assert_eq!(layout::installed_version(&f.root).as_deref(), Some("3.1.0"));
    let again = layout::install_crow_package(&p, &f.root).unwrap();
    assert!(again.contains("up to date"), "{again}");
    assert!(f.root.join("venv").is_dir(), "the venv is the installer's, never removed");
}

/// The real crow-nest pack, when `CROWSETUP_ENGINE_PACK` names one (skipped
/// otherwise): it installs into bin/ as its manifest says, serve executable.
/// Nothing is run.
#[test]
fn the_real_engine_pack_installs_when_given() {
    let Some(pack) = std::env::var_os("CROWSETUP_ENGINE_PACK").map(PathBuf::from) else {
        eprintln!("CROWSETUP_ENGINE_PACK not set: skipped");
        return;
    };
    let f = fx();
    let msg = layout::install_engine_package(&pack, &f.root).unwrap();
    assert!(msg.contains("installed"), "{msg}");
    let manifest = layout::parse_manifest(&read(&f.root.join("bin/MANIFEST.json"))).unwrap();
    for e in &manifest {
        let p = f.root.join("bin").join(&e.path);
        assert_eq!(fs::metadata(&p).unwrap().len(), e.bytes, "{}", e.path);
    }
    assert!(manifest.iter().any(|e| e.path == "serve"));
    assert_eq!(mode(&f.root.join("bin/serve")) & 0o111, 0o111, "serve is not executable");
    eprintln!("{msg}");
}
