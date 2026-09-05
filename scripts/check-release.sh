#!/usr/bin/env bash
# Validate the exact artifact path users receive from PyPI. No publish occurs.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dist_dir="$repo_root/dist"
temp_dir=$(mktemp -d)
trap 'rm -rf "$temp_dir"' EXIT

rm -rf "$dist_dir"
uv build --out-dir "$dist_dir" "$repo_root"

sdist=$(find "$dist_dir" -maxdepth 1 -name 'flpkit-*.tar.gz' -print -quit)
test -n "$sdist"
rebuilt_dist="$temp_dir/rebuilt-dist"
uv build --wheel --out-dir "$rebuilt_dist" "$sdist"

wheel=$(find "$rebuilt_dist" -maxdepth 1 -name 'flpkit-*.whl' -print -quit)
test -n "$wheel"
venv_dir="$temp_dir/venv"
uv venv "$venv_dir"
uv pip install --python "$venv_dir/bin/python" "$wheel"

golden="$repo_root/tests/golden/tempo-patch-in-place.flp"
sample="$temp_dir/roundtrip.flp"
cp "$golden" "$sample"
(
    cd "$temp_dir"
    "$venv_dir/bin/python" - "$sample" <<'PY'
from pathlib import Path
import sys

import flpkit

path = Path(sys.argv[1])
project = flpkit.read(path)
if project.tempo is None:
    raise RuntimeError("golden fixture must contain an explicit tempo")
saved_tempo = flpkit.set_tempo(path, project.tempo)
round_tripped = flpkit.read(path)
assert round_tripped.tempo == saved_tempo
print(f"flpkit {flpkit.__version__}: imported and round-tripped {path.name} at {saved_tempo:g} BPM")
PY
)
