//! T2: selection -> fetch list. Every number is checked twice: as a literal and
//! against stack.json's own `bytes` blocks, so a manifest change that forgets the
//! plan (or the other way round) goes red here.

use crowsetup_core::api::{FileKind, Package, Packages, Selection};
use crowsetup_core::plan::plan;
use crowsetup_core::stack::{Stack, Status};
use std::path::{Path, PathBuf};

const CROW_PKG: u64 = 41_000_000;
const ENGINE_PKG: u64 = 310_000_000;
/// config.json + vocabulary.txt + tokenizer.json + model.bin of faster-whisper-small.
const WHISPER: u64 = 486_212_372;
const PKGS: u64 = CROW_PKG + ENGINE_PKG;
/// The upstream text_encoder/ (index + 4 shards), deleted after te_rename.
const TEXT_ENCODER: u64 = 17_534_407_283;
const SDCLI: u64 = 17_534_419_188;

fn packages() -> Packages {
    Packages {
        crow: Package {
            asset: "crow-3.0.0-win-x64.zip".into(),
            url: "https://github.com/nibor1896/Crow/releases/download/v3.0.0/crow-3.0.0-win-x64.zip".into(),
            bytes: CROW_PKG,
            sha256: "AA".repeat(32),
            version: "3.0.0".into(),
        },
        engine: Package {
            asset: "crow-nest-engine-0.9.0-win-x64.zip".into(),
            url: "https://github.com/nibor1896/crow-nest/releases/download/v0.9.0/crow-nest-engine-0.9.0-win-x64.zip".into(),
            bytes: ENGINE_PKG,
            sha256: "bb".repeat(32),
            version: "0.9.0".into(),
        },
    }
}

fn sel(points: &[&str], install: &Path) -> Selection {
    Selection { points: points.iter().map(|s| s.to_string()).collect(), install_root: install.to_path_buf(), shortcut_dir: None }
}

fn default_root() -> PathBuf {
    PathBuf::from(r"C:\Crow")
}

fn plan_for(points: &[&str]) -> crowsetup_core::api::Plan {
    let root = default_root();
    plan(&Stack::embedded(), &sel(points, &root), &root.join("models"), &packages()).expect("plan")
}

fn point_files(id: &str) -> u64 {
    Stack::embedded().point(id).unwrap().bytes.files
}

fn ids(p: &crowsetup_core::api::Plan) -> Vec<&str> {
    p.jobs.iter().map(|j| j.id.as_str()).collect()
}

#[test]
fn flash_next_alone() {
    let p = plan_for(&["flash-next"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 9);
    assert_eq!(point_files("flash-next"), 105_644_572_827);
    assert_eq!(p.download_bytes, PKGS + WHISPER + 105_644_572_827);
    assert_eq!(p.download_bytes, PKGS + WHISPER + point_files("flash-next"));
    assert_eq!(p.disk_bytes, p.download_bytes);
    assert!(p.derived.is_empty());
    assert_eq!(p.jobs.last().unwrap().id, "fn-cnq");
}

#[test]
fn twenty_seven_b_alone() {
    let p = plan_for(&["27b"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 7);
    assert_eq!(p.download_bytes, PKGS + WHISPER + 18_784_665_622);
    assert_eq!(p.download_bytes, PKGS + WHISPER + point_files("27b"));
    assert_eq!(p.disk_bytes, p.download_bytes);
    assert!(p.derived.is_empty());
}

#[test]
fn image_stack_alone_derives_sdcli_and_drops_the_text_encoder() {
    let p = plan_for(&["image-stack"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 17);
    assert_eq!(p.download_bytes, PKGS + WHISPER + 51_900_384_939);
    assert_eq!(p.download_bytes, PKGS + WHISPER + point_files("image-stack"));
    assert_eq!(p.derived.len(), 1);
    let d = &p.derived[0];
    assert_eq!(d.id, "qi-text-encoder-sdcli");
    assert_eq!(d.bytes, SDCLI);
    assert_eq!(d.bytes, Stack::embedded().point("image-stack").unwrap().bytes.derived);
    assert_eq!(d.points, vec!["image-stack".to_string()]);
    assert_eq!(d.dest, default_root().join("models").join("qwen-image-2.1").join("text_encoder_sdcli"));
    // download + derived - the upstream text_encoder/ deleted after conversion
    assert_eq!(p.disk_bytes, p.download_bytes + SDCLI - TEXT_ENCODER);
    assert_eq!(p.disk_bytes, PKGS + WHISPER + 51_900_384_939 + 11_905);
}

#[test]
fn twenty_seven_b_and_image_stack_share_the_27b_files() {
    let p = plan_for(&["27b", "image-stack"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 17, "the seven 27B files are fetched once");
    assert_eq!(p.download_bytes, PKGS + WHISPER + point_files("image-stack"));
    let mut seen = std::collections::HashSet::new();
    assert!(p.jobs.iter().all(|j| seen.insert(j.id.clone())), "no id twice: {:?}", ids(&p));
    for id in ["27b-cnq", "27b-sidecar", "27b-mmproj", "27b-tokenizer", "27b-tokenizer-config", "27b-sha256sums", "27b-license"] {
        let j = p.jobs.iter().find(|j| j.id == id).unwrap();
        assert_eq!(j.points, vec!["27b".to_string(), "image-stack".to_string()], "{id}");
    }
    let qi = p.jobs.iter().find(|j| j.id == "qi-vae").unwrap();
    assert_eq!(qi.points, vec!["image-stack".to_string()]);
    assert_eq!(p.disk_bytes, p.download_bytes + SDCLI - TEXT_ENCODER);
}

#[test]
fn image_stack_alone_lists_only_itself_on_the_27b_files() {
    let p = plan_for(&["image-stack"]);
    let j = p.jobs.iter().find(|j| j.id == "27b-cnq").unwrap();
    assert_eq!(j.points, vec!["image-stack".to_string()]);
}

#[test]
fn all_three() {
    let p = plan_for(&["image-stack", "flash-next", "27b"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 9 + 17);
    assert_eq!(p.download_bytes, PKGS + WHISPER + 105_644_572_827 + 51_900_384_939);
    assert_eq!(p.download_bytes, PKGS + WHISPER + point_files("flash-next") + point_files("image-stack"));
    assert_eq!(p.disk_bytes, p.download_bytes + SDCLI - TEXT_ENCODER);
    let sum: u64 = p.jobs.iter().map(|j| j.bytes).sum();
    assert_eq!(sum, p.download_bytes);
    // points are listed in stack.json order, not selection order
    let j = p.jobs.iter().find(|j| j.id == "27b-mmproj").unwrap();
    assert_eq!(j.points, vec!["27b".to_string(), "image-stack".to_string()]);
}

#[test]
fn no_point_is_crow_alone() {
    let p = plan_for(&[]);
    assert_eq!(ids(&p), vec!["crow-package", "engine-package", "whisper-config", "whisper-vocabulary", "whisper-tokenizer", "whisper-model"]);
    assert_eq!(p.download_bytes, PKGS + WHISPER);
    assert_eq!(p.disk_bytes, PKGS + WHISPER);
}

#[test]
fn order_packages_then_whisper_then_models_smallest_first() {
    let p = plan_for(&["27b", "image-stack", "flash-next"]);
    assert_eq!(p.jobs[0].kind, FileKind::CrowPackage);
    assert_eq!(p.jobs[1].kind, FileKind::EnginePackage);
    assert_eq!(&ids(&p)[2..6], &["whisper-config", "whisper-vocabulary", "whisper-tokenizer", "whisper-model"]);
    assert!(p.jobs[2..6].iter().all(|j| j.kind == FileKind::Whisper));
    let models = &p.jobs[6..];
    assert!(models.iter().all(|j| j.kind == FileKind::Model));
    assert!(models.windows(2).all(|w| (w[0].bytes, &w[0].id) <= (w[1].bytes, &w[1].id)), "{:?}", ids(&p));
    assert_eq!(models.first().unwrap().id, "27b-sha256sums");
    assert_eq!(models.first().unwrap().bytes, 266);
    assert_eq!(models.last().unwrap().id, "fn-cnq");
    // equal sizes break by id: the two tokenizer.json copies
    let a = ids(&p).iter().position(|i| *i == "27b-tokenizer").unwrap();
    let b = ids(&p).iter().position(|i| *i == "fn-tokenizer").unwrap();
    assert_eq!(b, a + 1);
}

#[test]
fn packages_are_jobs_with_release_paths() {
    let root = PathBuf::from(r"D:\Apps\CrowTest");
    let p = plan(&Stack::embedded(), &sel(&["27b"], &root), &root.join("models"), &packages()).unwrap();
    let c = &p.jobs[0];
    assert_eq!(c.id, "crow-package");
    assert_eq!(c.url, packages().crow.url);
    assert_eq!(c.local_rel, "releases/crow-3.0.0-win-x64.zip");
    assert_eq!(c.dest, root.join("setup").join("downloads").join("crow-3.0.0-win-x64.zip"));
    assert_eq!(c.bytes, CROW_PKG);
    assert_eq!(c.sha256, "aa".repeat(32), "sha256 is lower-cased");
    assert!(c.points.is_empty());
    let e = &p.jobs[1];
    assert_eq!(e.id, "engine-package");
    assert_eq!(e.local_rel, "releases/crow-nest-engine-0.9.0-win-x64.zip");
    assert_eq!(e.dest, root.join("setup").join("downloads").join("crow-nest-engine-0.9.0-win-x64.zip"));
}

#[test]
fn dest_resolution_with_a_non_default_install_and_models_root() {
    // built with join so the test holds on both platforms (#342)
    let install = PathBuf::from(r"D:\Apps\CrowTest");
    let models = PathBuf::from(r"E:\big\models");
    let p = plan(&Stack::embedded(), &sel(&["image-stack"], &install), &models, &packages()).unwrap();
    let dest = |id: &str| p.jobs.iter().find(|j| j.id == id).unwrap().dest.clone();
    let under = |root: &Path, parts: &[&str]| parts.iter().fold(root.to_path_buf(), |p, x| p.join(x));
    assert_eq!(dest("27b-cnq"), under(&models, &["Qwen3.8-27B-CNQ4.5", "Qwen3.8-27B-CNQ4.5.cnq"]));
    assert_eq!(
        dest("qi-transformer-1"),
        under(&models, &["qwen-image-2.1", "transformer", "diffusion_pytorch_model-00001-of-00002.safetensors"])
    );
    // ${INSTALL}, not ${MODELS}: crow_voice.py loads <crow>/models/whisper-small
    assert_eq!(dest("whisper-model"), under(&install, &["models", "whisper-small", "model.bin"]));
    assert_eq!(p.derived[0].dest, under(&models, &["qwen-image-2.1", "text_encoder_sdcli"]));
}

#[test]
fn urls_are_pinned_and_local_rel_is_repo_path() {
    let p = plan_for(&["flash-next", "image-stack"]);
    let job = |id: &str| p.jobs.iter().find(|j| j.id == id).unwrap().clone();
    assert!(p.jobs.iter().all(|j| !j.url.contains("/resolve/main/")));
    let cnq = job("fn-cnq");
    assert_eq!(
        cnq.url,
        "https://huggingface.co/nibor1896/Qwen3.8-Flash-Next-CNQ4.5-M/resolve/3ebead456fbff1ceebe600fddc83cb13106b06ce/Qwen3.8-Flash-Next-CNQ4.5-M.cnq"
    );
    assert_eq!(cnq.local_rel, "nibor1896/Qwen3.8-Flash-Next-CNQ4.5-M/Qwen3.8-Flash-Next-CNQ4.5-M.cnq");
    assert_eq!(cnq.sha256, "7c058e555667b1c3d8f7d804d4a3393ba3664161dd307305718d85e4f33646b7");
    let qi = job("qi-text-encoder-1");
    assert_eq!(
        qi.url,
        "https://huggingface.co/Qwen/Qwen-Image-2.1/resolve/d26bb61231c349cf6b7896fa83353113880e1ba3/text_encoder/model-00001-of-00004.safetensors"
    );
    assert_eq!(qi.local_rel, "Qwen/Qwen-Image-2.1/text_encoder/model-00001-of-00004.safetensors");
    let w = job("whisper-tokenizer");
    assert_eq!(
        w.url,
        "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/tokenizer.json"
    );
    assert_eq!(w.local_rel, "Systran/faster-whisper-small/tokenizer.json");
    assert!(w.points.is_empty());
}

#[test]
fn foreign_files_come_from_their_original_repo_and_only_our_hotset_is_mirror_pending() {
    // #340, the owner 2026-10-03: only our own work goes into our repos. The
    // projectors and tokenizers are fetched from unsloth and Qwen, under their
    // own repo path; the crow-nest hotset is ours and waits for our repo.
    let p = plan_for(&["flash-next", "27b"]);
    let job = |id: &str| p.jobs.iter().find(|j| j.id == id).unwrap().clone();
    let mm = job("27b-mmproj");
    assert_eq!(
        mm.url,
        "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/4ca720788d1e01f1bff70c033e0d0028fd02e502/mmproj-F16.gguf"
    );
    assert_eq!(mm.local_rel, "unsloth/Qwen3.8-27B-GGUF/mmproj-F16.gguf");
    assert_eq!(mm.bytes, 927_607_488);
    let tok = job("fn-tokenizer");
    assert_eq!(
        tok.url,
        "https://huggingface.co/Qwen/Qwen3.8-Flash-Next/resolve/de4b8e4d43b917e7706784d8bb445c9af86a3540/tokenizer.json"
    );
    assert_eq!(tok.local_rel, "Qwen/Qwen3.8-Flash-Next/tokenizer.json");
    let hot = job("fn-hotsets-crow0924");
    assert_eq!(
        hot.url,
        "https://raw.githubusercontent.com/nibor1896/crow-nest/f4a3bd86f7b33db883f59c251cc04b37ac1ceeda/decode_out/hotsets-M-crow0924-n160.json"
    );
    assert_eq!(hot.local_rel, "nibor1896/Qwen3.8-Flash-Next-CNQ4.5-M/hotsets-M-crow0924-n160.json");
    // every model file that is not in our repo is a foreign original or the hotset
    let elsewhere: Vec<&str> = p.jobs.iter().filter(|j| !j.url.contains("/nibor1896/Qwen3.8-") && j.kind == FileKind::Model).map(|j| j.id.as_str()).collect();
    assert_eq!(elsewhere.len(), 7, "{elsewhere:?}");
    let s = Stack::embedded();
    let pending: Vec<&str> = s.files.iter().filter(|f| f.status == Status::MirrorPending).map(|f| f.id.as_str()).collect();
    assert_eq!(pending, ["fn-hotsets-crow0924"]);
    let (fnx, b27) = (s.point("flash-next").unwrap(), s.point("27b").unwrap());
    assert_eq!((fnx.bytes.mirror_pending, b27.bytes.mirror_pending), (37_167, 0));
    assert_eq!(fnx.bytes.upstream + b27.bytes.upstream, 916_868_415 - 37_167 + 940_434_736);
}

#[test]
fn unknown_point_is_an_error_and_a_repeated_one_is_not() {
    let root = default_root();
    let err = plan(&Stack::embedded(), &sel(&["9b"], &root), &root.join("models"), &packages()).unwrap_err();
    assert!(err.contains("9b"), "{err}");
    let p = plan_for(&["27b", "27b"]);
    assert_eq!(p.jobs.len(), 2 + 4 + 7);
    assert_eq!(p.jobs.iter().find(|j| j.id == "27b-cnq").unwrap().points, vec!["27b".to_string()]);
}

#[test]
fn embedded_stack_parses_with_its_crow_files() {
    let s = Stack::embedded();
    assert_eq!(s.points.iter().map(|p| p.id.as_str()).collect::<Vec<_>>(), ["flash-next", "27b", "image-stack"]);
    assert_eq!(s.files.len(), 26);
    assert_eq!(s.crow_files.len(), 4);
    assert_eq!(s.crow_files.iter().map(|f| f.bytes).sum::<u64>(), WHISPER);
    assert!(Stack::parse("{").is_err());
    let mut raw = s.raw.clone();
    raw["points"][1]["files"].as_array_mut().unwrap().push("27b-ghost".into());
    let err = Stack::parse(&raw.to_string()).unwrap_err();
    assert!(err.contains("27b-ghost"), "{err}");
}
