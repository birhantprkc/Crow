//! The command line: `--headless`, `--selftest`, `--source`, `--packages`,
//! `--install-root`, `--points`.

use std::path::PathBuf;

pub const USAGE: &str = "\
CrowSetup [options]
  (no options)          the window
  --headless            no window: plain text progress, same run
  --points <ids>        with --headless: comma-separated points to install
                        (flash-next, 27b, image-stack); without it the saved
                        selection continues
  --selftest            no network, no window: the run order on fake steps and
                        the core checks; prints RESULT, exit 0/1
  --source <dir>        local files instead of Hugging Face / GitHub
  --packages <file>     packages.json (required unless built with `bundle`)
  --install-root <dir>  install here; the setup state goes to <dir>\\setup
  --help                this text";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Window,
    Headless,
    Selftest,
    Help,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub mode: Mode,
    pub source: Option<PathBuf>,
    pub packages: Option<PathBuf>,
    pub install_root: Option<PathBuf>,
    pub points: Vec<String>,
}

pub const POINTS: [&str; 3] = ["flash-next", "27b", "image-stack"];

pub fn parse<I: IntoIterator<Item = String>>(args: I) -> Result<Args, String> {
    let mut a = Args { mode: Mode::Window, source: None, packages: None, install_root: None, points: vec![] };
    let mut modes = Vec::new();
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        let mut value = |name: &str| it.next().filter(|v| !v.starts_with("--")).ok_or(format!("{name} needs a value"));
        match arg.as_str() {
            "--headless" => modes.push(Mode::Headless),
            "--selftest" => modes.push(Mode::Selftest),
            "--help" | "-h" | "/?" => modes.push(Mode::Help),
            "--source" => a.source = Some(PathBuf::from(value("--source")?)),
            "--packages" => a.packages = Some(PathBuf::from(value("--packages")?)),
            "--install-root" => a.install_root = Some(PathBuf::from(value("--install-root")?)),
            "--points" => {
                for p in value("--points")?.split(',').map(str::trim).filter(|p| !p.is_empty()) {
                    if !POINTS.contains(&p) {
                        return Err(format!("unknown point {p:?} (known: {})", POINTS.join(", ")));
                    }
                    if !a.points.iter().any(|x| x == p) {
                        a.points.push(p.to_string());
                    }
                }
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    if modes.contains(&Mode::Help) {
        a.mode = Mode::Help;
        return Ok(a);
    }
    match modes.as_slice() {
        [] => {}
        [m] => a.mode = *m,
        _ => return Err("--headless and --selftest exclude each other".into()),
    }
    if !a.points.is_empty() && a.mode != Mode::Headless {
        return Err("--points goes with --headless".into());
    }
    Ok(a)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(s: &str) -> Result<Args, String> {
        parse(s.split_whitespace().map(String::from))
    }

    #[test]
    fn no_arguments_is_the_window() {
        let a = p("").unwrap();
        assert_eq!(a.mode, Mode::Window);
        assert_eq!((a.source, a.packages, a.install_root), (None, None, None));
    }

    #[test]
    fn every_option_is_read() {
        let a = p(r"--headless --source C:\src --packages C:\p.json --install-root C:\root --points 27b,image-stack,27b")
            .unwrap();
        assert_eq!(a.mode, Mode::Headless);
        assert_eq!(a.source, Some(PathBuf::from(r"C:\src")));
        assert_eq!(a.packages, Some(PathBuf::from(r"C:\p.json")));
        assert_eq!(a.install_root, Some(PathBuf::from(r"C:\root")));
        assert_eq!(a.points, vec!["27b", "image-stack"]);
    }

    #[test]
    fn selftest_and_help() {
        assert_eq!(p("--selftest").unwrap().mode, Mode::Selftest);
        assert_eq!(p("--selftest --help").unwrap().mode, Mode::Help);
    }

    #[test]
    fn mistakes_are_refused() {
        assert!(p("--source").unwrap_err().contains("needs a value"));
        assert!(p("--source --headless").unwrap_err().contains("needs a value"));
        assert!(p("--frobnicate").unwrap_err().contains("unknown argument"));
        assert!(p("--headless --points 70b").unwrap_err().contains("unknown point"));
        assert!(p("--headless --selftest").unwrap_err().contains("exclude"));
        assert!(p("--points 27b").unwrap_err().contains("--headless"));
    }
}
