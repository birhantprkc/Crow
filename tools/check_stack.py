"""Hold manifests/stack.json to its own promises, offline; with --online, to Hugging Face.

WHAT THE MANIFEST IS. The one machine-readable description of the three
operating points (flash-next, 27b, image-stack) that the installer and the boot
script consume: every file with a pinned revision, bytes and sha256, the engine
env and argv, the ports, the readiness probe and the identity. A wrong number in
it is a download that never verifies or a serve that boots the wrong model, on a
machine that is not ours. So:

OFFLINE (the default, no network):
  * schema       - every required field, the right type, a known status;
  * paths        - every path starts with ${INSTALL} or ${MODELS}; no other
                   placeholder, no drive letter, no home directory, no user name
                   anywhere in the file (documentation keys included: they ship);
  * references   - every id a point names exists, every file and derived entry
                   is used by some point, a derived entry's inputs are in each
                   point that uses it;
  * sums         - each point's bytes per status, files, derived and disk equal
                   the sum of what it lists; preflight disk equals that sum;
  * wiring       - every env and argv path names a file or derived output the
                   SAME point installs; the identity is the CROW_CNQ file name;
                   the slot dir is created; ports agree with argv; a menu line
                   never claims more context than the point serves;
  * crow files   - the Crow-wide group (crow_files: the dictation model) has every
                   field, a pinned 40-hex revision, a status of published or
                   upstream, a dest under ${INSTALL}/ that no other file uses, and
                   holds model.bin under ${INSTALL}/models/whisper-small/, the
                   directory cli/crow_voice.py loads.

ONLINE (--online): bytes, sha256 and revision of every file re-read from the
source - the tree API at the pinned revision (lfs oid for LFS files, a download
of at most 1 MiB for small ones), raw GitHub for a GitHub source. A mirror-pending
file is checked at its source, and the planned location in our repo is looked
at: present and equal is a NOTE (flip it to published), present and different
is a failure. A repo whose HEAD moved past the pin is a NOTE: the pin still holds.
The crow_files group is re-read the same way at its pinned revision (a non-LFS
file there may be up to CROW_SMALL, the dictation tokenizer is 2.2 MB).

Usage:  check_stack.py [--manifest <file>] [--online]
Exit 0 = every check holds.  1 = at least one does not.  2 = setup error.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST = os.path.join(REPO, "manifests", "stack.json")

SCHEMA = "crow-stack/1"
POINT_IDS = ("flash-next", "27b", "image-stack")
STATUSES = ("published", "mirror-pending", "upstream")
PLACEHOLDERS = ("INSTALL", "MODELS")
FILE_FIELDS = ("id", "repo", "path", "revision", "bytes", "sha256", "dest", "source",
               "status", "license", "role")
SOURCE_FIELDS = ("host", "repo", "path", "revision", "bytes", "sha256", "license")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REPO_ID = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
PATH_ROOTS = ("${INSTALL}/", "${MODELS}/")
# absolute or personal paths: a drive letter, a UNC root, a home directory
PERSONAL = re.compile(r"(?i)(\b[a-z]:[\\/]|\\\\[a-z0-9]|(^|[\s\"'(=])~[\\/]|[\\/](users|home)[\\/])")
SMALL = 1 << 20
CROW_SMALL = 8 << 20
CROW_FILE_FIELDS = ("id", "repo", "path", "revision", "bytes", "sha256", "dest", "status",
                    "license", "role")
CROW_STATUSES = ("published", "upstream")
WHISPER_DIR = "${INSTALL}/models/whisper-small/"
HF = "https://huggingface.co"


def user_names() -> "list[str]":
    names = set()
    for var in ("USERNAME", "USER", "LOGNAME"):
        v = (os.environ.get(var) or "").strip()
        if len(v) >= 3:
            names.add(v.lower())
    home = os.path.basename(os.path.expanduser("~").rstrip("\\/"))
    if len(home) >= 3:
        names.add(home.lower())
    return sorted(names)


def strings(node, where=""):
    """Every (location, string) in the document, keys included."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield where + "/" + k, k
            yield from strings(v, where + "/" + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from strings(v, "%s[%d]" % (where, i))
    elif isinstance(node, str):
        yield where, node


def is_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.startswith("${")


class Report:
    def __init__(self):
        self.lines = []
        self.failed = 0
        self.total = 0

    def check(self, label: str, problems: "list[str]", ok_detail: str = ""):
        self.total += 1
        if problems:
            self.failed += 1
            self.lines.append("  FAIL     %-28s %s" % (label, problems[0]))
            for p in problems[1:]:
                self.lines.append("  %-37s %s" % ("", p))
        else:
            self.lines.append("  OK       %-28s %s" % (label, ok_detail))

    def note(self, text: str):
        self.lines.append("  NOTE     %s" % text)


def check_schema(doc) -> "list[str]":
    p = []
    if doc.get("schema") != SCHEMA:
        p.append("schema is %r, expected %r" % (doc.get("schema"), SCHEMA))
    ph = doc.get("placeholders")
    if not isinstance(ph, dict) or sorted(ph) != sorted(PLACEHOLDERS):
        p.append("placeholders must declare exactly %s" % ", ".join(PLACEHOLDERS))
    for key, kind in (("licenses", dict), ("files", list), ("derived", list), ("points", list)):
        if not isinstance(doc.get(key), kind):
            p.append("%s missing or not a %s" % (key, kind.__name__))
    if p:
        return p
    licenses = doc["licenses"]
    ids = {}
    for f in doc["files"]:
        fid = f.get("id", "?")
        missing = [k for k in FILE_FIELDS if k not in f]
        if missing:
            p.append("file %s lacks %s" % (fid, ", ".join(missing)))
            continue
        if fid in ids:
            p.append("file id %s twice" % fid)
        ids[fid] = f
        if f["status"] not in STATUSES:
            p.append("file %s status %r is not one of %s" % (fid, f["status"], ", ".join(STATUSES)))
        if not isinstance(f["bytes"], int) or isinstance(f["bytes"], bool) or f["bytes"] <= 0:
            p.append("file %s bytes %r is not a positive integer" % (fid, f["bytes"]))
        if not isinstance(f["sha256"], str) or not HEX64.match(f["sha256"]):
            p.append("file %s sha256 is not 64 lowercase hex" % fid)
        if not isinstance(f["repo"], str) or not REPO_ID.match(f["repo"]):
            p.append("file %s repo %r is not owner/name" % (fid, f["repo"]))
        if not isinstance(f["path"], str) or not f["path"] or f["path"].startswith("/"):
            p.append("file %s path %r is not repo-relative" % (fid, f["path"]))
        if f["license"] not in licenses:
            p.append("file %s licence %r is not declared under licenses" % (fid, f["license"]))
        if f["status"] in ("published", "upstream"):
            if not isinstance(f["revision"], str) or not HEX40.match(f["revision"]):
                p.append("file %s (%s) needs a 40-hex commit revision" % (fid, f["status"]))
            if f["source"] is not None:
                p.append("file %s (%s) carries a source; only mirror-pending does" % (fid, f["status"]))
        elif f["status"] == "mirror-pending":
            if f["revision"] is not None:
                p.append("file %s is mirror-pending: revision must be null until the upload" % fid)
            s = f["source"]
            if not isinstance(s, dict) or [k for k in SOURCE_FIELDS if k not in s]:
                p.append("file %s source needs %s" % (fid, ", ".join(SOURCE_FIELDS)))
            else:
                if s["host"] not in ("huggingface", "github"):
                    p.append("file %s source host %r unknown" % (fid, s["host"]))
                if not isinstance(s["revision"], str) or not HEX40.match(s["revision"]):
                    p.append("file %s source needs a 40-hex commit revision" % fid)
                if s["bytes"] != f["bytes"] or s["sha256"] != f["sha256"]:
                    p.append("file %s bytes/sha256 differ from its source's" % fid)
                if s["license"] not in licenses:
                    p.append("file %s source licence %r is not declared" % (fid, s["license"]))
    dests = {}
    for f in doc["files"]:
        d = f.get("dest")
        if d in dests:
            p.append("files %s and %s share dest %s" % (dests[d], f.get("id"), d))
        dests[d] = f.get("id")
    for name, lic in licenses.items():
        for k in ("name", "non_commercial", "show_at_install", "text_file", "summary"):
            if k not in lic:
                p.append("licence %s lacks %s" % (name, k))
        if lic.get("non_commercial") and not lic.get("show_at_install"):
            p.append("licence %s is non-commercial but not shown at install" % name)
        tf = ids.get(lic.get("text_file"))
        if tf is None or tf.get("role") != "license":
            p.append("licence %s text_file %r is not a file with role license"
                     % (name, lic.get("text_file")))
    for d in doc["derived"]:
        for k in ("id", "tool", "argv", "inputs", "dest", "outputs", "bytes"):
            if k not in d:
                p.append("derived %s lacks %s" % (d.get("id", "?"), k))
    for pt in doc["points"]:
        for k in ("id", "menu", "files", "derived", "bytes", "engine", "image_server",
                  "crow_env", "preflight"):
            if k not in pt:
                p.append("point %s lacks %s" % (pt.get("id", "?"), k))
    got = [pt.get("id") for pt in doc["points"]]
    if got != list(POINT_IDS):
        p.append("points are %s, expected %s" % (got, list(POINT_IDS)))
    return p


def check_paths(doc) -> "list[str]":
    p = []
    names = user_names()
    for where, s in strings(doc):
        for name in PLACEHOLDER.findall(s):
            if name not in PLACEHOLDERS:
                p.append("%s: unknown placeholder ${%s}" % (where, name))
        if PERSONAL.search(s):
            p.append("%s: absolute or personal path in %r" % (where, s[:80]))
        low = s.lower()
        for n in names:
            if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(n), low):
                p.append("%s: names the user %r" % (where, n))
    for f in doc["files"]:
        if not str(f.get("dest", "")).startswith(PATH_ROOTS):
            p.append("file %s dest %r does not start with ${INSTALL}/ or ${MODELS}/"
                     % (f.get("id"), f.get("dest")))
    for d in doc["derived"]:
        for v in [d.get("dest")] + list(d.get("argv") or []):
            if not str(v).startswith(PATH_ROOTS):
                p.append("derived %s path %r is not under a placeholder" % (d.get("id"), v))
    for pt in doc["points"]:
        for label, values in path_values(pt):
            for v in values:
                if is_path(v) and not v.startswith(PATH_ROOTS) and v != "${INSTALL}":
                    p.append("point %s %s %r is not under a placeholder" % (pt.get("id"), label, v))
    return p


def path_values(pt):
    eng = pt.get("engine") or {}
    yield "engine.binary", list((eng.get("binary") or {}).values())
    yield "engine.cwd", [eng.get("cwd", "")]
    yield "engine.env", list((eng.get("env") or {}).values())
    yield "engine.argv", list(eng.get("argv") or [])
    yield "engine.dirs", list(eng.get("dirs") or [])
    img = pt.get("image_server")
    if img:
        yield "image_server.binary", list((img.get("binary") or {}).values())
        yield "image_server.argv", list(img.get("argv") or [])
    yield "crow_env", list((pt.get("crow_env") or {}).values())


def check_references(doc) -> "list[str]":
    p = []
    files = {f["id"]: f for f in doc["files"]}
    derived = {d["id"]: d for d in doc["derived"]}
    used_f, used_d = set(), set()
    for d in derived.values():
        for i in d["inputs"]:
            if i not in files:
                p.append("derived %s input %s is not a file" % (d["id"], i))
    for pt in doc["points"]:
        for fid in pt["files"]:
            if fid not in files:
                p.append("point %s names file %s, which is not declared" % (pt["id"], fid))
            used_f.add(fid)
        if len(set(pt["files"])) != len(pt["files"]):
            p.append("point %s names a file twice" % pt["id"])
        for did in pt["derived"]:
            if did not in derived:
                p.append("point %s names derived %s, which is not declared" % (pt["id"], did))
                continue
            used_d.add(did)
            for i in derived[did]["inputs"]:
                if i not in pt["files"]:
                    p.append("point %s derives %s without its input %s" % (pt["id"], did, i))
    for fid in files:
        if fid not in used_f:
            p.append("file %s is used by no point" % fid)
    for did in derived:
        if did not in used_d:
            p.append("derived %s is used by no point" % did)
    return p


def point_sums(doc, pt) -> dict:
    files = {f["id"]: f for f in doc["files"]}
    derived = {d["id"]: d for d in doc["derived"]}
    s = {"published": 0, "mirror_pending": 0, "upstream": 0}
    for fid in pt["files"]:
        f = files[fid]
        s[f["status"].replace("-", "_")] += f["bytes"]
    s["files"] = s["published"] + s["mirror_pending"] + s["upstream"]
    s["derived"] = sum(derived[d]["bytes"] for d in pt["derived"])
    s["disk"] = s["files"] + s["derived"]
    return s


def check_sums(doc) -> "list[str]":
    p = []
    for d in doc["derived"]:
        total = sum(o["bytes"] for o in d["outputs"])
        if total != d["bytes"]:
            p.append("derived %s bytes %d, its outputs sum to %d" % (d["id"], d["bytes"], total))
    for pt in doc["points"]:
        want = point_sums(doc, pt)
        got = pt["bytes"]
        for k, v in want.items():
            if got.get(k) != v:
                p.append("point %s bytes.%s is %r, the listed files sum to %d"
                         % (pt["id"], k, got.get(k), v))
        if pt["preflight"].get("disk_bytes") != want["disk"]:
            p.append("point %s preflight.disk_bytes is %r, files + derived = %d"
                     % (pt["id"], pt["preflight"].get("disk_bytes"), want["disk"]))
    return p


def flag_value(argv, flag):
    if flag in argv and argv.index(flag) + 1 < len(argv):
        return argv[argv.index(flag) + 1]
    return None


def check_wiring(doc) -> "list[str]":
    p = []
    files = {f["id"]: f for f in doc["files"]}
    derived = {d["id"]: d for d in doc["derived"]}
    for pt in doc["points"]:
        pid = pt["id"]
        installed = {files[f]["dest"]: files[f] for f in pt["files"] if f in files}
        produced = set()
        for did in pt["derived"]:
            d = derived.get(did)
            if d:
                produced.update(d["dest"] + "/" + o["path"] for o in d["outputs"])
        eng = pt["engine"]
        dirs = set(eng.get("dirs") or [])
        env = eng.get("env") or {}
        for k, v in env.items():
            if is_path(v) and v not in installed:
                p.append("point %s env %s=%s is no file this point installs" % (pid, k, v))
        cnq = installed.get(env.get("CROW_CNQ"))
        if cnq is None or cnq.get("role") != "container":
            p.append("point %s CROW_CNQ does not name its container" % pid)
        else:
            ident = eng.get("identity") or {}
            if ident.get("path") != "/props" or ident.get("field") != "model_path" \
                    or ident.get("endswith") != cnq["path"].rsplit("/", 1)[-1]:
                p.append("point %s identity must be /props model_path ending in %s"
                         % (pid, cnq["path"].rsplit("/", 1)[-1]))
        ready = eng.get("readiness") or {}
        if ready.get("path") != "/health" or ready.get("json") != {"status": "ok"}:
            p.append("point %s readiness must be GET /health answering {\"status\": \"ok\"}" % pid)
        argv = list(eng.get("argv") or [])
        if flag_value(argv, "--port") != str(eng.get("port")):
            p.append("point %s engine --port %r differs from port %r"
                     % (pid, flag_value(argv, "--port"), eng.get("port")))
        slot = flag_value(argv, "--slot-save-path")
        if slot is None or slot not in dirs:
            p.append("point %s --slot-save-path %r is not created (engine.dirs)" % (pid, slot))
        for v in argv:
            if is_path(v) and v not in dirs and v not in installed:
                p.append("point %s engine argv path %s is neither a created dir nor a file" % (pid, v))
        img = pt["image_server"]
        if img:
            iargv = list(img.get("argv") or [])
            if flag_value(iargv, "--listen-port") != str(img.get("port")):
                p.append("point %s sd-server --listen-port differs from port %r" % (pid, img.get("port")))
            if img.get("port") == eng.get("port"):
                p.append("point %s engine and image server share port %r" % (pid, img.get("port")))
            if (img.get("readiness") or {}).get("status") != 200:
                p.append("point %s image readiness must expect HTTP 200" % pid)
            for v in iargv:
                if is_path(v) and v not in installed and v not in produced:
                    p.append("point %s sd-server path %s is neither installed nor derived" % (pid, v))
        for k, v in (pt.get("crow_env") or {}).items():
            if is_path(v) and not any(x == v or x.startswith(v + "/") for x in list(installed) + list(produced)):
                p.append("point %s crow_env %s=%s holds nothing this point installs" % (pid, k, v))
        line = (pt.get("menu") or {}).get("line", "")
        if not line:
            p.append("point %s has no menu line" % pid)
        for k in re.findall(r"(\d+)k context", line):
            if int(k) * 1000 > int(eng.get("context") or 0):
                p.append("point %s menu claims %sk context, the engine serves %r"
                         % (pid, k, eng.get("context")))
        # #196: the operating-point window's own line (cli/crow_boot_gui.py shows
        # it; the terminal menu keeps `line`). Sentences, no dash; its context
        # claim is held to the engine like the menu's. A user name in it is the
        # "placeholders and paths" check's, which walks every string.
        gui = (pt.get("menu") or {}).get("gui", "")
        if not gui:
            p.append("point %s has no menu gui text (the operating-point window's line)" % pid)
        if "\u2014" in gui or "\u2013" in gui:
            p.append("point %s menu gui text has a dash; the window writes sentences" % pid)
        for k in re.findall(r"(\d+)k context", gui):
            if int(k) * 1000 > int(eng.get("context") or 0):
                p.append("point %s menu gui text claims %sk context, the engine serves %r"
                         % (pid, k, eng.get("context")))
    return p


def check_crow_files(doc) -> "list[str]":
    group = doc.get("crow_files")
    if not isinstance(group, list) or not group:
        return ["crow_files missing or empty (the dictation model every install gets)"]
    p = []
    file_ids = {f.get("id") for f in doc.get("files") or []}
    dests = {f.get("dest"): f.get("id") for f in doc.get("files") or []}
    seen = set()
    for f in group:
        if not isinstance(f, dict):
            p.append("crow_files entry %r is not an object" % (f,))
            continue
        fid = f.get("id", "?")
        missing = [k for k in CROW_FILE_FIELDS if k not in f]
        if missing:
            p.append("crow file %s lacks %s" % (fid, ", ".join(missing)))
            continue
        if fid in seen or fid in file_ids:
            p.append("crow file id %s twice (crow_files and files share one id space)" % fid)
        seen.add(fid)
        if f["status"] not in CROW_STATUSES:
            p.append("crow file %s status %r is not one of %s" % (fid, f["status"], ", ".join(CROW_STATUSES)))
        if not isinstance(f["revision"], str) or not HEX40.match(f["revision"]):
            p.append("crow file %s needs a 40-hex commit revision" % fid)
        if not isinstance(f["bytes"], int) or isinstance(f["bytes"], bool) or f["bytes"] <= 0:
            p.append("crow file %s bytes %r is not a positive integer" % (fid, f["bytes"]))
        if not isinstance(f["sha256"], str) or not HEX64.match(f["sha256"]):
            p.append("crow file %s sha256 is not 64 lowercase hex" % fid)
        if not isinstance(f["repo"], str) or not REPO_ID.match(f["repo"]):
            p.append("crow file %s repo %r is not owner/name" % (fid, f["repo"]))
        if not isinstance(f["path"], str) or not f["path"] or f["path"].startswith("/"):
            p.append("crow file %s path %r is not repo-relative" % (fid, f["path"]))
        if not isinstance(f["license"], str) or not f["license"]:
            p.append("crow file %s has no licence" % fid)
        d = f["dest"]
        if not str(d).startswith("${INSTALL}/"):
            p.append("crow file %s dest %r does not start with ${INSTALL}/" % (fid, d))
        if d in dests:
            p.append("crow file %s and %s share dest %s" % (fid, dests[d], d))
        dests[d] = fid
    if WHISPER_DIR + "model.bin" not in {f.get("dest") for f in group if isinstance(f, dict)}:
        p.append("crow_files holds no %smodel.bin, the file cli/crow_voice.py loads by" % WHISPER_DIR)
    return p


# ---- online ----

def fetch(url: str, limit: int = 0, tries: int = 16):
    """(body, headers); Hugging Face resets connections, so retry with pauses."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crow-check-stack"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read(limit + 1) if limit else r.read()
                if limit and len(body) > limit:
                    raise ValueError("%s is larger than %d bytes" % (url, limit))
                return body, r.headers
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise
            last = e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
        time.sleep(min(2 + 3 * i, 20))
    raise RuntimeError("giving up on %s: %s" % (url, last))


class Hub:
    def __init__(self):
        self.trees = {}
        self.heads = {}

    def head(self, repo):
        if repo not in self.heads:
            body, _ = fetch("%s/api/models/%s" % (HF, repo))
            self.heads[repo] = json.loads(body)["sha"]
        return self.heads[repo]

    def tree(self, repo, rev):
        key = (repo, rev)
        if key not in self.trees:
            url = "%s/api/models/%s/tree/%s?recursive=true&expand=true" % (HF, repo, rev)
            entries = {}
            while url:
                body, headers = fetch(url)
                for e in json.loads(body):
                    if e.get("type") == "file":
                        entries[e["path"]] = e
                url = None
                for part in (headers.get("Link") or "").split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
            self.trees[key] = entries
        return self.trees[key]

    def measure(self, repo, rev, path, limit=SMALL):
        """(bytes, sha256) at the source, or raise."""
        e = self.tree(repo, rev).get(path)
        if e is None:
            raise LookupError("%s@%s has no %s" % (repo, rev[:12], path))
        if e.get("lfs"):
            return e["size"], e["lfs"]["oid"]
        body, _ = fetch("%s/%s/resolve/%s/%s" % (HF, repo, rev, path), limit=limit)
        return len(body), hashlib.sha256(body).hexdigest()


def github_measure(repo, rev, path):
    body, _ = fetch("https://raw.githubusercontent.com/%s/%s/%s" % (repo, rev, path), limit=SMALL)
    return len(body), hashlib.sha256(body).hexdigest()


def check_online(doc, report: Report):
    hub = Hub()
    for f in doc["files"]:
        problems = []
        try:
            if f["status"] == "mirror-pending":
                s = f["source"]
                if s["host"] == "github":
                    got = github_measure(s["repo"], s["revision"], s["path"])
                else:
                    got = hub.measure(s["repo"], s["revision"], s["path"])
                where = "%s:%s@%s" % (s["host"], s["repo"], s["revision"][:12])
                try:
                    ours = hub.measure(f["repo"], hub.head(f["repo"]), f["path"])
                except LookupError:
                    ours = None
                if ours is not None:
                    if ours == (f["bytes"], f["sha256"]):
                        report.note("%s is already in %s: flip it to published" % (f["id"], f["repo"]))
                    else:
                        problems.append("%s at %s/%s differs from its source" % (f["id"], f["repo"], f["path"]))
            else:
                got = hub.measure(f["repo"], f["revision"], f["path"])
                where = "%s@%s" % (f["repo"], f["revision"][:12])
                if hub.head(f["repo"]) != f["revision"]:
                    report.note("%s: %s HEAD is %s, pinned %s"
                                % (f["id"], f["repo"], hub.head(f["repo"])[:12], f["revision"][:12]))
            if got != (f["bytes"], f["sha256"]):
                problems.append("%s: source has %d B %s, the manifest %d B %s"
                                % (f["id"], got[0], got[1][:12], f["bytes"], f["sha256"][:12]))
        except Exception as e:  # a failed fetch is a failed check, named
            problems.append("%s: %s" % (f["id"], e))
            where = "?"
        report.check("online " + f["id"], problems, "%d B at %s" % (f["bytes"], where))
    check_online_crow(doc, report, hub)


def check_online_crow(doc, report: Report, hub):
    for f in doc.get("crow_files") or []:
        problems = []
        where = "%s@%s" % (f["repo"], f["revision"][:12])
        try:
            got = hub.measure(f["repo"], f["revision"], f["path"], limit=CROW_SMALL)
            if got != (f["bytes"], f["sha256"]):
                problems.append("%s: source has %d B %s, the manifest %d B %s"
                                % (f["id"], got[0], got[1][:12], f["bytes"], f["sha256"][:12]))
            if hub.head(f["repo"]) != f["revision"]:
                report.note("%s: %s HEAD is %s, pinned %s"
                            % (f["id"], f["repo"], hub.head(f["repo"])[:12], f["revision"][:12]))
        except Exception as e:  # a failed fetch is a failed check, named
            problems.append("%s: %s" % (f["id"], e))
        report.check("online " + f["id"], problems, "%d B at %s" % (f["bytes"], where))


def run(doc, online=False) -> Report:
    r = Report()
    schema = check_schema(doc)
    r.check("schema", schema, "%d files, %d derived, %d points"
            % (len(doc.get("files") or []), len(doc.get("derived") or []), len(doc.get("points") or [])))
    if schema:
        return r  # the other checks index fields the schema did not find
    r.check("placeholders and paths", check_paths(doc), "only ${INSTALL}/${MODELS}, nothing personal")
    refs = check_references(doc)
    r.check("references", refs, "every file and derived entry used, every id resolves")
    if refs:
        return r
    r.check("byte sums", check_sums(doc), "; ".join(
        "%s %d files %s B" % (pt["id"], len(pt["files"]), format(pt["bytes"]["disk"], ","))
        for pt in doc["points"]))
    r.check("engine wiring", check_wiring(doc), "env/argv paths are files of their own point")
    crow = [f for f in doc.get("crow_files") or [] if isinstance(f, dict)]
    crow_problems = check_crow_files(doc)
    r.check("crow files", crow_problems, "%d files %s B, dictation in %s"
            % (len(crow), format(sum(f.get("bytes") or 0 for f in crow), ","), WHISPER_DIR))
    if crow_problems:
        return r  # the online half indexes the fields this check did not find
    if online:
        check_online(doc, r)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--online", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.manifest, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print("check_stack: cannot read %s: %s" % (a.manifest, e), file=sys.stderr)
        return 2
    r = run(doc, a.online)
    print("\n".join(r.lines))
    print()
    name = os.path.relpath(a.manifest, REPO).replace("\\", "/") if a.manifest == MANIFEST else a.manifest
    if r.failed:
        print("RESULT: %d of %d checks fail against %s" % (r.failed, r.total, name))
        return 1
    print("RESULT: %d of %d checks hold against %s" % (r.total, r.total, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
