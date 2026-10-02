//! T2: what this machine can run (GPU via nvidia-smi, RAM, disk, WebView2, 64-bit).
//!
//! Probing ([`probe`]) is split from judging ([`verdicts`]) and from parsing
//! ([`parse_nvidia_smi`]), so the rules are tested on fixtures.
//!
//! Rules:
//! - `hard_block` (nothing can be installed, one sentence): not 64-bit Windows,
//!   no NVIDIA GPU reported by nvidia-smi, or compute capability major != 12
//!   (crow-nest builds its FP4 path for compute_120a only, docs/getting-started.md).
//!   With a hard block, `blocked` stays empty: the sentence says it once.
//! - per point, at most one reason, checked in this order:
//!   VRAM below 32 GB (every point; RTX 5090 = 32,607 MiB), host RAM below 64 GB
//!   for a point whose stack.json `preflight.host_ram.pinned_max_gib` is set
//!   (Flash-Next pins up to 46 GiB), free disk below the point's own
//!   `preflight.disk_bytes` (files + derived, the peak during conversion).
//! - GB in sentences is GiB, as Explorer shows it; needs round up, free space
//!   rounds down. A 64 GB host shows about 63.4 GiB, so RAM counts as 64 GB from
//!   60 GiB on; VRAM counts as 32 GB from 30 GiB on.
//! - WebView2 missing is not a block: `--headless` is the fallback.
//! - Linux (#342): RAM from /proc/meminfo, free disk from statvfs (f_bavail,
//!   what an unprivileged user may write), and `webview2` reports WebKitGTK 4.1
//!   (the window links it, so a binary that runs has it). The OS sentence names
//!   Linux.

use crate::api::{Blocked, PreflightReport};
use crate::stack::Stack;
use std::path::{Path, PathBuf};

const GIB: u64 = 1 << 30;
const RAM_NEED_GB: u64 = 64;
const RAM_FLOOR_BYTES: u64 = 60 * GIB;
const VRAM_NEED_GB: u64 = 32;
const VRAM_FLOOR_MIB: u64 = 30 * 1024;
const COMPUTE_MAJOR: u32 = 12;

/// The WebView2 Evergreen Runtime client GUID (Microsoft Learn, "Distribute your
/// app and the WebView2 Runtime", Detect if a WebView2 Runtime is already installed).
pub const WEBVIEW2_CLIENT: &str = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Gpu {
    pub name: String,
    pub vram_mib: Option<u64>,
    /// e.g. "12.0".
    pub compute_cap: Option<String>,
}

impl Gpu {
    fn compute_major(&self) -> Option<u32> {
        self.compute_cap.as_deref()?.split('.').next()?.parse().ok()
    }
}

/// The machine as probed, before any judgement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Facts {
    pub os_64bit: bool,
    pub gpus: Vec<Gpu>,
    pub ram_bytes: u64,
    pub disk_free_bytes: u64,
    pub webview2: bool,
}

/// Lines of `nvidia-smi --query-gpu=name,memory.total,compute_cap
/// --format=csv,noheader,nounits`. A line without three fields ("No devices were
/// found", an error text) is skipped; `[N/A]` reads as None.
pub fn parse_nvidia_smi(out: &str) -> Vec<Gpu> {
    out.lines()
        .filter_map(|line| {
            // the name may hold a comma; the two numbers never do
            let mut it = line.trim().rsplitn(3, ',');
            let cc = it.next()?.trim();
            let mem = it.next()?.trim();
            let name = it.next()?.trim();
            let vram_mib = mem.parse::<u64>().ok();
            let valid_cc = cc.split_once('.').is_some_and(|(a, b)| {
                !a.is_empty() && !b.is_empty() && a.chars().chain(b.chars()).all(|c| c.is_ascii_digit())
            });
            let compute_cap = valid_cc.then(|| cc.to_string());
            if name.is_empty() || (vram_mib.is_none() && compute_cap.is_none()) {
                return None;
            }
            Some(Gpu { name: name.to_string(), vram_mib, compute_cap })
        })
        .collect()
}

/// The card Crow will run on: a compute 12.x card first, then the most VRAM,
/// then nvidia-smi's order.
pub fn pick_gpu(gpus: &[Gpu]) -> Option<&Gpu> {
    gpus.iter().enumerate().max_by_key(|(i, g)| (g.compute_major() == Some(COMPUTE_MAJOR), g.vram_mib.unwrap_or(0), std::cmp::Reverse(*i))).map(|(_, g)| g)
}

fn ceil_gib(bytes: u64) -> u64 {
    bytes.div_ceil(GIB)
}

/// "an NVIDIA ..." (spoken en-vidia), "a Quadro ...", "an AMD ...".
fn article(name: &str) -> &'static str {
    if name.starts_with("NVIDIA") {
        return "an";
    }
    match name.chars().next().map(|c| c.to_ascii_uppercase()) {
        Some('A' | 'E' | 'I' | 'O' | 'U') => "an",
        _ => "a",
    }
}

/// The OS the hard-block sentence names.
const OS_NAME: &str = if cfg!(windows) { "Windows" } else { "Linux" };

pub fn verdicts(facts: &Facts, stack: &Stack) -> PreflightReport {
    verdicts_with_disk(facts, stack, &|p: &crate::stack::Point| p.preflight.disk_bytes)
}

/// [`verdicts`] with each point's disk need handed in: a `--source` folder on the
/// same Linux file system is hard-linked, not copied, so it costs no space (#342).
pub fn verdicts_with_disk(facts: &Facts, stack: &Stack, disk_need: &dyn Fn(&crate::stack::Point) -> u64) -> PreflightReport {
    let gpu = pick_gpu(&facts.gpus);
    let hard_block = if !facts.os_64bit {
        Some(format!("Crow needs 64-bit {OS_NAME}, and this machine runs 32-bit {OS_NAME}."))
    } else {
        match gpu {
            None => Some("Crow needs an NVIDIA RTX 50 series GPU, and nvidia-smi reports none on this machine.".to_string()),
            Some(g) if g.compute_major() != Some(COMPUTE_MAJOR) => Some(format!(
                "Crow needs an NVIDIA RTX 50 series GPU, and this machine has {} {} (compute {}).",
                article(&g.name),
                g.name,
                g.compute_cap.as_deref().unwrap_or("unknown")
            )),
            Some(_) => None,
        }
    };

    let mut blocked = Vec::new();
    if hard_block.is_none() {
        for p in &stack.points {
            let vram = gpu.and_then(|g| g.vram_mib);
            let reason = if let Some(mib) = vram.filter(|m| *m < VRAM_FLOOR_MIB) {
                Some(format!("Needs {VRAM_NEED_GB} GB of VRAM. This GPU has {} GB.", mib.div_ceil(1024)))
            } else if p.preflight.host_ram.pinned_max_gib.is_some() && facts.ram_bytes < RAM_FLOOR_BYTES {
                Some(format!("Needs {RAM_NEED_GB} GB RAM. This machine has {} GB.", ceil_gib(facts.ram_bytes)))
            } else if facts.disk_free_bytes < disk_need(p) {
                Some(format!(
                    "Needs {} GB free disk. This drive has {} GB free.",
                    ceil_gib(disk_need(p)),
                    facts.disk_free_bytes / GIB
                ))
            } else {
                None
            };
            if let Some(reason) = reason {
                blocked.push(Blocked { point: p.id.clone(), reason });
            }
        }
    }

    PreflightReport {
        os_64bit: facts.os_64bit,
        gpu_name: gpu.map(|g| g.name.clone()),
        vram_mib: gpu.and_then(|g| g.vram_mib),
        compute_cap: gpu.and_then(|g| g.compute_cap.clone()),
        ram_bytes: facts.ram_bytes,
        disk_free_bytes: facts.disk_free_bytes,
        webview2: facts.webview2,
        hard_block,
        blocked,
    }
}

/// A WebView2 `pv` value means installed when it is a version above 0.0.0.0.
pub fn webview2_pv_installed(pv: Option<&str>) -> bool {
    pv.map(str::trim).is_some_and(|v| !v.is_empty() && v != "0.0.0.0")
}

/// The path itself or its nearest ancestor that exists (the install root is
/// usually not created yet when preflight runs).
pub fn nearest_existing(path: &Path) -> Option<PathBuf> {
    path.ancestors().find(|p| !p.as_os_str().is_empty() && p.exists()).map(Path::to_path_buf)
}

/// Free bytes on the volume of `install_root` (its nearest existing ancestor),
/// without the rest of the probe: run.rs checks space before every large file.
pub fn disk_free(install_root: &Path) -> u64 {
    nearest_existing(install_root).map(|p| sys::disk_free(&p)).unwrap_or(0)
}

/// Reads this machine. Never fails: what cannot be read is None / 0 / false.
pub fn probe(install_root: &Path) -> Facts {
    Facts {
        os_64bit: sys::os_64bit(),
        gpus: nvidia_smi().map(|o| parse_nvidia_smi(&o)).unwrap_or_default(),
        ram_bytes: sys::ram_total(),
        disk_free_bytes: disk_free(install_root),
        webview2: sys::webview2(),
    }
}

pub fn run(stack: &Stack, install_root: &Path) -> PreflightReport {
    verdicts(&probe(install_root), stack)
}

/// nvidia-smi's stdout; None when the binary is absent or cannot start.
fn nvidia_smi() -> Option<String> {
    let mut cmd = std::process::Command::new("nvidia-smi");
    cmd.args(["--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000; // no console flash from the GUI exe
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let out = cmd.output().ok()?;
    Some(String::from_utf8_lossy(&out.stdout).into_owned())
}

#[cfg(windows)]
mod sys {
    use super::{WEBVIEW2_CLIENT, webview2_pv_installed};
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;
    use windows_sys::Win32::Foundation::ERROR_SUCCESS;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    use windows_sys::Win32::System::Registry::{HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, RRF_RT_REG_SZ, RegGetValueW};
    use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};

    fn wide(s: &std::ffi::OsStr) -> Vec<u16> {
        s.encode_wide().chain(std::iter::once(0)).collect()
    }

    /// A 64-bit build only starts on 64-bit Windows; a 32-bit build sees
    /// PROCESSOR_ARCHITEW6432 under WOW64.
    pub fn os_64bit() -> bool {
        cfg!(target_pointer_width = "64") || std::env::var_os("PROCESSOR_ARCHITEW6432").is_some()
    }

    pub fn ram_total() -> u64 {
        let mut m: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
        m.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        // SAFETY: m is a properly sized, writable MEMORYSTATUSEX with dwLength set.
        if unsafe { GlobalMemoryStatusEx(&mut m) } != 0 { m.ullTotalPhys } else { 0 }
    }

    pub fn disk_free(dir: &Path) -> u64 {
        let w = wide(dir.as_os_str());
        let mut free = 0u64;
        // SAFETY: w is NUL-terminated; the two unused outputs may be null.
        let ok = unsafe { GetDiskFreeSpaceExW(w.as_ptr(), &mut free, std::ptr::null_mut(), std::ptr::null_mut()) };
        if ok != 0 { free } else { 0 }
    }

    fn reg_sz(root: HKEY, subkey: &str, value: &str) -> Option<String> {
        let k = wide(std::ffi::OsStr::new(subkey));
        let v = wide(std::ffi::OsStr::new(value));
        let mut buf = [0u16; 256];
        let mut size = std::mem::size_of_val(&buf) as u32;
        // SAFETY: read-only query; buf is writable and size is its length in bytes.
        let rc = unsafe {
            RegGetValueW(root, k.as_ptr(), v.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), buf.as_mut_ptr().cast(), &mut size)
        };
        if rc != ERROR_SUCCESS {
            return None;
        }
        let n = (size as usize / 2).min(buf.len());
        let s = String::from_utf16_lossy(&buf[..n]);
        Some(s.trim_end_matches('\0').to_string())
    }

    /// Per-machine (64-bit Windows: WOW6432Node) or per-user Evergreen runtime.
    pub fn webview2() -> bool {
        let machine = format!(r"SOFTWARE\WOW6432Node\{WEBVIEW2_CLIENT}");
        let user = format!(r"Software\{WEBVIEW2_CLIENT}");
        webview2_pv_installed(reg_sz(HKEY_LOCAL_MACHINE, &machine, "pv").as_deref())
            || webview2_pv_installed(reg_sz(HKEY_CURRENT_USER, &user, "pv").as_deref())
    }
}

/// `MemTotal` of /proc/meminfo, in bytes; 0 when absent.
pub fn parse_meminfo(text: &str) -> u64 {
    text.lines()
        .find_map(|l| l.strip_prefix("MemTotal:"))
        .and_then(|rest| rest.split_whitespace().next()?.parse::<u64>().ok())
        .map_or(0, |kib| kib * 1024)
}

/// Where a WebKitGTK 4.1 runtime library lives on the common distributions.
pub const WEBKITGTK_LIBS: [&str; 4] = [
    "/usr/lib/libwebkit2gtk-4.1.so.0",
    "/usr/lib64/libwebkit2gtk-4.1.so.0",
    "/usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0",
    "/usr/local/lib/libwebkit2gtk-4.1.so.0",
];

#[cfg(unix)]
mod sys {
    //! Linux (#342).
    use std::path::Path;
    pub fn os_64bit() -> bool {
        cfg!(target_pointer_width = "64")
    }
    pub fn ram_total() -> u64 {
        std::fs::read_to_string("/proc/meminfo").map(|t| super::parse_meminfo(&t)).unwrap_or(0)
    }
    pub fn disk_free(dir: &Path) -> u64 {
        use std::os::unix::ffi::OsStrExt;
        let Ok(c) = std::ffi::CString::new(dir.as_os_str().as_bytes()) else { return 0 };
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        // SAFETY: c is NUL-terminated, st is a writable statvfs.
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            return 0;
        }
        (st.f_bavail as u64).saturating_mul(st.f_frsize as u64)
    }
    /// WebKitGTK 4.1, the Linux WebView.
    pub fn webview2() -> bool {
        super::WEBKITGTK_LIBS.iter().any(|p| Path::new(p).exists())
    }
}

#[cfg(not(any(windows, unix)))]
mod sys {
    use std::path::Path;
    pub fn os_64bit() -> bool {
        cfg!(target_pointer_width = "64")
    }
    pub fn ram_total() -> u64 {
        0
    }
    pub fn disk_free(_dir: &Path) -> u64 {
        0
    }
    pub fn webview2() -> bool {
        false
    }
}
