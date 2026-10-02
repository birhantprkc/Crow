//! `--selftest`: no network, no window. The run order on the fake steps, the
//! embedded stack and a plan per point of it (and that cli::POINTS names the
//! same points), and the embedded UI. Prints one line
//! per check and a RESULT line; exit 0 when every check passes.

use crowsetup_core::api::{Package, Packages};
use crowsetup_core::run::testing;
use crowsetup_core::stack::Stack;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;

/// A check that may hit an unfinished module: a panic is a failure, not a crash.
fn guarded(f: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(r) => r,
        Err(p) => Err(format!(
            "panicked: {}",
            p.downcast_ref::<&str>().map(|s| s.to_string()).or_else(|| p.downcast_ref::<String>().cloned()).unwrap_or_default()
        )),
    }
}

fn package(asset: &str) -> Package {
    Package {
        asset: asset.into(),
        url: format!("https://example.invalid/{asset}"),
        bytes: 1,
        sha256: "0".repeat(64),
        version: "0".into(),
    }
}

pub fn main() -> i32 {
    std::panic::set_hook(Box::new(|_| {}));
    let mut checks: Vec<(String, Result<(), String>)> =
        testing::scenarios().into_iter().map(|(n, r)| (n.to_string(), r)).collect();

    checks.push((
        "embedded stack.json parses".into(),
        guarded(|| Stack::parse(crowsetup_core::STACK_JSON).map(|_| ())),
    ));
    let pkgs = Packages { crow: package("crow.zip"), engine: package("engine.zip") };
    // The points come from the embedded stack.json, not from cli::POINTS: a point
    // added there and forgotten in the CLI list is planned here and turns red below.
    let points: Vec<(String, bool)> = Stack::parse(crowsetup_core::STACK_JSON)
        .map(|s| s.points.iter().map(|p| (p.id.clone(), !p.derived.is_empty())).collect())
        .unwrap_or_default();
    let ids: Vec<&str> = points.iter().map(|(id, _)| id.as_str()).collect();
    checks.push((
        "points: stack.json = cli::POINTS".into(),
        if ids == crate::cli::POINTS {
            Ok(())
        } else {
            Err(format!("stack.json {ids:?}, cli::POINTS {:?}", crate::cli::POINTS))
        },
    ));
    for (point, has_derived) in &points {
        let (point, has_derived) = (point.as_str(), *has_derived);
        checks.push((
            format!("plan {point}"),
            guarded(|| {
                let stack = Stack::parse(crowsetup_core::STACK_JSON)?;
                let sel = testing::selection(&[point], Path::new(r"C:\selftest"));
                let plan = crowsetup_core::plan::plan(&stack, &sel, Path::new(r"C:\selftest\models"), &pkgs)?;
                let mut ids: Vec<&str> = plan.jobs.iter().map(|j| j.id.as_str()).collect();
                let n = ids.len();
                ids.sort();
                ids.dedup();
                if ids.len() != n {
                    return Err("a file is listed twice".into());
                }
                if has_derived == plan.derived.is_empty() {
                    return Err(format!("derived files: {}", plan.derived.len()));
                }
                Ok(())
            }),
        ));
    }
    checks.push(("embedded UI".into(), crate::window::check_page()));

    let red = checks.iter().filter(|(_, r)| r.is_err()).count();
    for (name, r) in &checks {
        match r {
            Ok(()) => println!("ok   {name}"),
            Err(e) => println!("FAIL {name}: {e}"),
        }
    }
    if red == 0 {
        println!("RESULT: OK {} checks", checks.len());
        0
    } else {
        println!("RESULT: FAIL {red} of {} checks", checks.len());
        1
    }
}
