//! T1: resumable download of one [`FileJob`] (Range + If-Range, unbounded
//! retries, stall detection, `.part` + 64 MB checkpoints, sha256 before rename).

use crate::api::{Event, FileJob, Source};
use crate::state::StateStore;
use std::sync::atomic::AtomicBool;

#[derive(Debug, Clone)]
pub struct FetchOptions {
    pub source: Source,
    /// Seconds without a byte before the connection is dropped (spec: 30).
    pub stall_secs: u64,
    /// Bytes between fsync + state checkpoints (spec: 64 MiB).
    pub checkpoint_bytes: u64,
    /// Backoff cap in seconds (spec: 30).
    pub max_backoff_secs: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum FetchError {
    /// 401/403/404 and friends: not retried in a loop, the UI offers Retry.
    #[error("{0}")]
    Permanent(String),
    #[error("sha256 mismatch for {id}: expected {expected}, got {got}")]
    Mismatch { id: String, expected: String, got: String },
    #[error("cancelled")]
    Cancelled,
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

/// Fetch `job` to `job.dest`, resuming from `<dest>.part` when present.
pub fn download(
    _job: &FileJob,
    _state: &mut StateStore,
    _opts: &FetchOptions,
    _on: &mut dyn FnMut(Event),
    _cancel: &AtomicBool,
) -> Result<(), FetchError> {
    unimplemented!("T1")
}
