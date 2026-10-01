//! #196 P2-E2E fix 2: `--package-source <dir>`: Crow's package and the engine
//! package come from `<dir>/<asset>` (sha256 and size from packages.json),
//! every other file from its remote URL.

mod fetch_support;

use crowsetup_core::api::{FileKind, Package, Packages, Source};
use crowsetup_core::run::{RealSteps, Steps};
use crowsetup_core::state::StateStore;
use fetch_support::*;
use std::sync::atomic::AtomicBool;

fn package(asset: &str, url: &str, content: &[u8]) -> Package {
    Package { asset: asset.into(), url: url.into(), bytes: content.len() as u64, sha256: sha_hex(content), version: "1".into() }
}

#[test]
fn packages_come_from_the_package_source_and_models_from_remote() {
    let zip = data(70_000, 41);
    let model = data(50_000, 42);
    let m2 = model.clone();
    // The remote: 404 for every package URL, the model file by Range.
    let srv = TestServer::start(move |_, seen, rq| {
        if seen.url.ends_with("model.bin") {
            serve_range(seen, rq, &m2, "\"m\"")
        } else {
            let _ = rq.respond(tiny_http::Response::empty(404));
        }
    });
    let tmp = tempfile::tempdir().unwrap();
    let pk_dir = tmp.path().join("packages");
    std::fs::create_dir_all(&pk_dir).unwrap();
    std::fs::write(pk_dir.join("crow-1-win-x64.zip"), &zip).unwrap();
    let packages = Packages {
        crow: package("crow-1-win-x64.zip", &format!("{}/crow.zip", srv.base), &zip),
        engine: package("crow-nest-engine-1-win-x64.zip", &format!("{}/engine.zip", srv.base), &zip),
    };
    let mut steps = RealSteps::new(Source::Remote, packages, None, None).with_package_source(Some(pk_dir.clone()));
    let mut st = StateStore::load(&tmp.path().join("state.json")).unwrap();
    let cancel = AtomicBool::new(false);

    let dest = tmp.path().join("install").join("setup").join("downloads").join("crow-1-win-x64.zip");
    let mut crow = job("crow-package", &format!("{}/crow.zip", srv.base), &dest, &zip);
    crow.kind = FileKind::CrowPackage;
    crow.local_rel = "releases/crow-1-win-x64.zip".into();
    steps.download(&crow, &mut st, &mut |_| {}, &cancel).unwrap();
    assert_eq!(std::fs::read(&dest).unwrap(), zip);

    let mdest = tmp.path().join("install").join("models").join("model.bin");
    let m = job("m", &format!("{}/model.bin", srv.base), &mdest, &model);
    steps.download(&m, &mut st, &mut |_| {}, &cancel).unwrap();
    assert_eq!(std::fs::read(&mdest).unwrap(), model);
    assert!(srv.seen().iter().all(|s| !s.url.ends_with("crow.zip")), "the package URL was asked: {:?}", srv.seen().iter().map(|s| &s.url).collect::<Vec<_>>());
}
