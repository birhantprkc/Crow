"""Read manifests/operating-point.json from a Python tool.

WHY: the sampling temperature stood in six files. Five of them had it because
somebody copied a working probe, and none of the five knew why 0.6 rather than 0
-- the reasoning lived in a comment in cli/crow.py that they had not copied. When
0731 moves the value, six edits have to agree or the probes measure something the
client never does.

DELIBERATELY TINY. It is a reader, not a framework: the manifest is JSON, the
callers are three scripts, and anything more would be a second thing to keep in
step with the first.

The manifest is read fresh on every call. Caching it would make the counter-probe
for this stage -- set the central value, watch every consumer change -- report the
old value on the second run inside one process.

utf-8-sig, not utf-8: PowerShell 5.1 writes JSON with a BOM, and this file is
edited from both sides.
"""

import json
import os

MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "manifests", "operating-point.json")


def load(path=MANIFEST):
    if not os.path.exists(path):
        raise FileNotFoundError("operating point manifest not found: %s" % path)
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def sampling(key, path=MANIFEST):
    """One sampling default. An unknown key raises, naming what exists.

    A reader that returns None for a typo turns into a request without the field,
    which the server answers happily with its own default -- and the run looks
    like it measured what was asked for.
    """
    block = load(path).get("sampling")
    if not block:
        raise KeyError("manifest has no 'sampling' block: %s" % path)
    if key not in block:
        known = sorted(k for k in block if not k.startswith("_"))
        raise KeyError("unknown sampling key %r. The manifest has: %s" % (key, ", ".join(known)))
    return block[key]


def model_path(key, path=MANIFEST, absolute=True):
    """The counterpart to Get-ModelPath in tools/model-paths.ps1."""
    models = load(path).get("models")
    if not models or key not in models.get("entries", {}):
        known = sorted((models or {}).get("entries", {}))
        raise KeyError("unknown model key %r. The table has: %s" % (key, ", ".join(known)))
    rel = models["entries"][key]["path"]
    if not absolute:
        return "models/" + rel
    return os.path.join(models["_root"].replace("/", os.sep), rel.replace("/", os.sep))
