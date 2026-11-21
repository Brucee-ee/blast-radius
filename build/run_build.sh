#!/bin/sh
# Rebuilds data/map.json. Needs: python3.13, pip install -r <kit>/requirements.lock coverage==7.17.4,
# a checkout of sqlglot at eb11aec4be8cb2122afe713a4816662c3f0fb2fa, and the kit's sample/tests.json.
set -e
SQLGLOT=${1:?path to sqlglot checkout}; TESTS_JSON=${2:?path to tests.json}; WORK=${3:-/tmp/brwork}
HERE=$(cd "$(dirname "$0")" && pwd); mkdir -p "$WORK"; rm -f "$WORK"/cov.db* "$WORK"/classes.* "$WORK"/attrs.* "$WORK"/opens.json
cp "$HERE/covplugin.py" "$HERE/sitecustomize.py" "$HERE/brhooks.py" "$WORK/"
sed "s#data_file = .*#data_file = $WORK/cov.db#" "$HERE/coveragerc" > "$WORK/.coveragerc"
cd "$SQLGLOT"
export BR_ATTRS=1 COV_CLASSES_DIR="$WORK" SKIP_INTEGRATION=0 PYTHONPATH="$SQLGLOT:$WORK" OPENS_OUT="$WORK/opens.json" COVERAGE_PROCESS_START="$WORK/.coveragerc"
coverage run --rcfile="$WORK/.coveragerc" -m pytest -p covplugin -q -p no:cacheprovider tests --timeout=600
coverage combine --rcfile="$WORK/.coveragerc"
python "$HERE/build_map.py" "$WORK" "$SQLGLOT" "$TESTS_JSON" unused
