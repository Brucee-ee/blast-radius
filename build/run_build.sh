#!/bin/sh
# Rebuilds data/map.json from sqlglot at eb11aec4be8cb2122afe713a4816662c3f0fb2fa.
# Needs python3.13 with the kit's requirements.lock and coverage==7.17.4 installed.
#   build/run_build.sh <sqlglot checkout> <tests.json> [work dir]
set -e
SQLGLOT=${1:?path to sqlglot checkout}; TESTS_JSON=${2:?path to tests.json}; WORK=${3:-/tmp/brwork}
HERE=$(cd "$(dirname "$0")" && pwd)
COV="$WORK/cov"; PROF="$WORK/prof"
mkdir -p "$COV" "$PROF"
rm -f "$COV"/cov.db* "$COV"/classes.* "$COV"/attrs.* "$COV"/opens.json "$COV"/components.json "$PROF"/prof.*
for d in "$COV" "$PROF"; do cp "$HERE/covplugin.py" "$HERE/sitecustomize.py" "$HERE/brhooks.py" "$HERE/brprofile.py" "$d/"; done
sed "s#data_file = .*#data_file = $COV/cov.db#" "$HERE/coveragerc" > "$COV/.coveragerc"
cd "$SQLGLOT"
export SKIP_INTEGRATION=0
# 1. per-test line coverage, class usage, fixture reads and class-attribute reads
BR_ATTRS=1 COV_CLASSES_DIR="$COV" PYTHONPATH="$SQLGLOT:$COV" OPENS_OUT="$COV/opens.json" \
  COVERAGE_PROCESS_START="$COV/.coveragerc" \
  coverage run --rcfile="$COV/.coveragerc" -m pytest -p covplugin -q -p no:cacheprovider tests --timeout=1800 || true
coverage combine --rcfile="$COV/.coveragerc"
# 2. comparison profile
BR_PROFILE=1 COV_CLASSES_DIR="$PROF" PYTHONPATH="$SQLGLOT:$PROF" \
  python -m pytest -p covplugin -q -p no:cacheprovider tests --timeout=3600
BR_PROF_DIR="$PROF" python "$HERE/build_map.py" "$COV" "$SQLGLOT" "$TESTS_JSON" unused
python "$HERE/pack.py"
