#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-pathtracer-kit.sh -- rebuild kits/pathtracer/crow-pathtracer.js
# ---------------------------------------------------------------------------
#
# WHAT
#   The one vendored browser library in this repository: three.js, three-mesh-bvh
#   and three-gpu-pathtracer, bundled by esbuild into ONE minified ES module.
#   The voxel kit (kits/pathtracer/voxel-kit.js) imports it, and build_bundle
#   inlines both into the agent's single offline page (#298).
#
# WHY VENDORED AND NOT FETCHED AT RUN TIME
#   Crow never downloads at run time, and a page opened from file:// cannot
#   import a sibling module anyway. The model is not asked to assemble the
#   library either: re-typed library bytes are exactly what #91 corrupted.
#
# PINS (exact, never ranges). Change them here, re-run, commit the new bundle,
# kit.json and the NOTICE entry together -- tools/check_pathtracer_kit.py holds
# all three against each other.
THREE_VERSION="0.186.1"
BVH_VERSION="0.9.15"
PT_VERSION="0.0.24"
ESBUILD_VERSION="0.28.2"
#
# USAGE
#   bash tools/build-pathtracer-kit.sh [SCRATCH_DIR]
#   Needs node + npm and the network (the npm registry). Writes only into
#   SCRATCH_DIR (default: a mktemp dir) and kits/pathtracer/. node_modules
#   stays in the scratch dir and is never committed.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT="$REPO/kits/pathtracer"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
command -v npm >/dev/null 2>&1 || { echo "error: npm is needed to rebuild the bundle" >&2; exit 1; }

cd "$WORK"
[ -f package.json ] || npm init -y >/dev/null
npm install --no-audit --no-fund --save-exact \
    "three@$THREE_VERSION" "three-mesh-bvh@$BVH_VERSION" \
    "three-gpu-pathtracer@$PT_VERSION" "esbuild@$ESBUILD_VERSION" >/dev/null

# The entry: three's whole namespace plus the path tracer's exports, side by
# side at the top level. Measured 2026-09-25: 444 three exports, 18 path-tracer
# exports, 0 names in common -- so `import * as THREE from 'crow-pathtracer'`
# is three.js, and WebGLPathTracer & co. sit beside it. three-mesh-bvh is not
# re-exported; the path tracer bundles the parts it uses.
cat > entry.js <<'EOF'
export * from 'three';
export * from 'three-gpu-pathtracer';
EOF

./node_modules/.bin/esbuild entry.js --bundle --format=esm --platform=browser \
    --minify --charset=utf8 --legal-comments=eof --log-level=warning \
    --outfile="$WORK/crow-pathtracer.js"

mkdir -p "$KIT"
cp "$WORK/crow-pathtracer.js" "$KIT/crow-pathtracer.js"
# The licence texts BYTE-IDENTICAL, copied out of the npm tarballs -- the rule
# patches/LICENSE.llama.cpp follows: a licence retyped in transit is worse
# than none.
cp node_modules/three/LICENSE                "$KIT/LICENSE.three"
cp node_modules/three-mesh-bvh/LICENSE       "$KIT/LICENSE.three-mesh-bvh"
cp node_modules/three-gpu-pathtracer/LICENSE "$KIT/LICENSE.three-gpu-pathtracer"

# kit.json: what was built from what. The integrity strings are npm's own
# (package-lock.json), so a rebuild can be checked against the registry.
node - "$KIT" <<'EOF'
const fs = require('fs'), crypto = require('crypto'), path = require('path');
const kit = process.argv[2];
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8')).packages;
const sha = (f) => crypto.createHash('sha256').update(fs.readFileSync(path.join(kit, f))).digest('hex');
const pkg = (n) => ({ version: lock['node_modules/' + n].version,
                      integrity: lock['node_modules/' + n].integrity,
                      license: JSON.parse(fs.readFileSync('node_modules/' + n + '/package.json')).license });
const bundle = 'crow-pathtracer.js';
const out = {
  bundle: { file: bundle, bytes: fs.statSync(path.join(kit, bundle)).size, sha256: sha(bundle),
            format: 'esm', minified: true, built_by: 'tools/build-pathtracer-kit.sh' },
  esbuild: lock['node_modules/esbuild'].version,
  packages: { 'three': pkg('three'), 'three-mesh-bvh': pkg('three-mesh-bvh'),
              'three-gpu-pathtracer': pkg('three-gpu-pathtracer') },
  licenses: { 'three': 'LICENSE.three', 'three-mesh-bvh': 'LICENSE.three-mesh-bvh',
              'three-gpu-pathtracer': 'LICENSE.three-gpu-pathtracer' },
  license_sha256: Object.fromEntries(['LICENSE.three', 'LICENSE.three-mesh-bvh',
                                      'LICENSE.three-gpu-pathtracer'].map((f) => [f, sha(f)])),
};
fs.writeFileSync(path.join(kit, 'kit.json'), JSON.stringify(out, null, 2) + '\n');
console.log(`${bundle}: ${out.bundle.bytes} bytes, sha256 ${out.bundle.sha256}`);
EOF
