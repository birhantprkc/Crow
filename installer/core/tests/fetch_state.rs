//! T1 (#196): `state.json` (atomic, tolerant) and the streaming sha256.

use crowsetup_core::api::Selection;
use crowsetup_core::state::{FileState, StateStore};
use crowsetup_core::verify::{Sha256Stream, sha256_file};

#[test]
fn state_round_trips_and_leaves_no_temp_file() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("Crow").join("setup").join("state.json");
    let mut st = StateStore::load(&path).unwrap();
    assert!(st.files.is_empty() && st.selection.is_none() && st.steps_done.is_empty());
    assert_eq!(st.path, path);

    st.selection = Some(Selection {
        points: vec!["27b".into()],
        install_root: tmp.path().join("Crow"),
        shortcut_dir: None,
    });
    st.files.insert(
        "27b-cnq".into(),
        FileState {
            etag: Some("\"e\"".into()),
            bytes_done: 67_108_864,
            verified: false,
        },
    );
    st.steps_done.push("crow".into());
    st.save().unwrap();

    let back = StateStore::load(&path).unwrap();
    assert_eq!(back.selection, st.selection);
    assert_eq!(back.files["27b-cnq"].bytes_done, 67_108_864);
    assert_eq!(back.files["27b-cnq"].etag.as_deref(), Some("\"e\""));
    assert_eq!(back.steps_done, vec!["crow".to_string()]);
    let names: Vec<_> = std::fs::read_dir(path.parent().unwrap())
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(names, vec!["state.json".to_string()], "no temp file left");

    // A second save replaces the first.
    st.steps_done.push("engine".into());
    st.save().unwrap();
    assert_eq!(StateStore::load(&path).unwrap().steps_done.len(), 2);
}

#[test]
fn corrupt_state_is_kept_as_bad_and_starts_empty() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("state.json");
    std::fs::write(&path, b"{\"files\": {\"x\": tru").unwrap();
    let st = StateStore::load(&path).unwrap();
    assert!(st.files.is_empty());
    assert_eq!(
        std::fs::read(tmp.path().join("state.json.bad")).unwrap(),
        b"{\"files\": {\"x\": tru"
    );
    st.save().unwrap();
    assert!(StateStore::load(&path).unwrap().files.is_empty());
}

#[test]
fn sha256_of_a_file_and_incrementally_agree() {
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("f");
    std::fs::write(&p, b"hello world").unwrap();
    let want = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9";
    assert_eq!(sha256_file(&p).unwrap(), want);

    let mut h = Sha256Stream::new();
    h.update(b"hello ");
    h.update(b"world");
    assert_eq!(h.finish(), want);

    let mut h = Sha256Stream::new();
    let n = h
        .update_reader(&mut std::fs::File::open(&p).unwrap())
        .unwrap();
    assert_eq!(n, 11);
    assert_eq!(h.finish(), want);
}
