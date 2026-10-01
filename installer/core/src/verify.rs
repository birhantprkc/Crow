//! T1: sha256 of files and streams.

use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::Path;

const BUF: usize = 1 << 20;

/// Incremental sha256: feed bytes as they arrive, read the hex at the end.
#[derive(Clone, Default)]
pub struct Sha256Stream(Sha256);

impl Sha256Stream {
    pub fn new() -> Self {
        Self(Sha256::new())
    }

    pub fn update(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    /// Hash everything `r` yields; returns the byte count.
    pub fn update_reader(&mut self, r: &mut dyn Read) -> std::io::Result<u64> {
        let mut buf = vec![0u8; BUF];
        let mut total = 0u64;
        loop {
            let n = match r.read(&mut buf) {
                Ok(0) => return Ok(total),
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            };
            self.0.update(&buf[..n]);
            total += n as u64;
        }
    }

    /// Lower-case hex.
    pub fn finish(self) -> String {
        hex::encode(self.0.finalize())
    }
}

/// Lower-case hex sha256 of a whole file.
pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut h = Sha256Stream::new();
    h.update_reader(&mut std::fs::File::open(path)?)?;
    Ok(h.finish())
}
