//! T1 (#196): `--source <dir>` copies with the same `.part`/resume/verify rules.

mod fetch_support;

use crowsetup_core::api::{Event, Source};
use crowsetup_core::fetch::{FetchError, download};
use crowsetup_core::state::{FileState, StateStore};
use fetch_support::*;
use std::sync::atomic::AtomicBool;

#[test]
fn local_source_resumes_an_existing_part_and_verifies() {
    let content = data(100_000, 21);
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("src");
    let dest = tmp.path().join("install").join("models").join("l.bin");
    let job = job("l", "https://unused.invalid/l.bin", &dest, &content);
    std::fs::create_dir_all(root.join("repo")).unwrap();
    std::fs::write(root.join(&job.local_rel), &content).unwrap();

    // A part from an earlier run: 30 kB checkpointed, 5 kB beyond the checkpoint.
    std::fs::create_dir_all(dest.parent().unwrap()).unwrap();
    std::fs::write(part_of(&dest), &content[..35_000]).unwrap();
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();
    st.files.insert(
        "l".into(),
        FileState {
            etag: None,
            bytes_done: 30_000,
            verified: false,
        },
    );
    st.save().unwrap();

    let mut ev = Vec::new();
    let cancel = AtomicBool::new(false);
    download(
        &job,
        &mut st,
        &opts(Source::Local(root.clone())),
        &mut |e| ev.push(e),
        &cancel,
    )
    .unwrap();

    assert_eq!(
        checking(&ev),
        Some(30_000),
        "resumes from the checkpoint, not the raw part length"
    );
    assert!(ev.iter().any(|e| matches!(
        e,
        Event::FileStarted {
            from_byte: 30_000,
            total: 100_000,
            ..
        }
    )));
    assert!(ev.iter().any(|e| matches!(
        e,
        Event::FileProgress {
            done: 100_000,
            total: 100_000,
            ..
        }
    )));
    assert_eq!(std::fs::read(&dest).unwrap(), content);
    assert!(!part_of(&dest).exists());
    assert!(verified(&ev, "l"));
    assert!(StateStore::load(&st.path).unwrap().files["l"].verified);
}

#[test]
fn local_source_with_wrong_bytes_is_a_mismatch_after_one_refetch() {
    let good = data(10_000, 22);
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("src");
    let dest = tmp.path().join("m.bin");
    let job = job("m", "https://unused.invalid/m.bin", &dest, &good);
    std::fs::create_dir_all(root.join("repo")).unwrap();
    std::fs::write(root.join(&job.local_rel), data(10_000, 23)).unwrap();
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();

    let mut ev = Vec::new();
    let r = download(
        &job,
        &mut st,
        &opts(Source::Local(root)),
        &mut |e| ev.push(e),
        &AtomicBool::new(false),
    );
    assert!(matches!(r, Err(FetchError::Mismatch { .. })), "{r:?}");
    assert_eq!(retries(&ev).len(), 1, "exactly one refetch");
    assert!(!dest.exists() && !part_of(&dest).exists());
}

#[test]
fn local_source_missing_file_is_permanent() {
    let content = data(100, 24);
    let tmp = tempfile::tempdir().unwrap();
    let dest = tmp.path().join("n.bin");
    let job = job("n", "https://unused.invalid/n.bin", &dest, &content);
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();
    let r = download(
        &job,
        &mut st,
        &opts(Source::Local(tmp.path().join("nowhere"))),
        &mut |_| {},
        &AtomicBool::new(false),
    );
    assert!(matches!(r, Err(FetchError::Permanent(_))), "{r:?}");
}

/// #196 P2-E2E: a verified file that is gone (deleted, or the image stack's
/// convert input) is fetched again; stopped part-way, the state must not
/// still say `verified`, or the welcome-back replay shows a 22 % file as done.
/// Measured on the real run: Quit at 4,001,628,160 of 17,844,005,845 bytes
/// left `"verified": true` in state.json.
#[test]
fn a_refetch_stopped_part_way_is_not_left_verified() {
    let content = data(12 << 20, 31);
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("src");
    let dest = tmp.path().join("install").join("v.bin");
    let job = job("v", "https://unused.invalid/v.bin", &dest, &content);
    std::fs::create_dir_all(root.join("repo")).unwrap();
    std::fs::write(root.join(&job.local_rel), &content).unwrap();
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();
    let cancel = AtomicBool::new(false);
    download(&job, &mut st, &opts(Source::Local(root.clone())), &mut |_| {}, &cancel).unwrap();
    assert!(st.files["v"].verified);

    std::fs::remove_file(&dest).unwrap();
    let total = content.len() as u64;
    let r = download(
        &job,
        &mut st,
        &opts(Source::Local(root.clone())),
        &mut |e| {
            if let Event::FileProgress { done, .. } = e
                && done < total
            {
                cancel.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        },
        &cancel,
    );
    assert!(matches!(r, Err(FetchError::Cancelled)), "{r:?}");
    let saved = StateStore::load(&st.path).unwrap().files["v"].clone();
    assert!(saved.bytes_done < total, "stopped part-way: {}", saved.bytes_done);
    assert!(!saved.verified, "a part of {} of {total} bytes is recorded as verified", saved.bytes_done);
}
