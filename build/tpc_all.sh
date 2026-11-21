#!/bin/sh
# Builds tpc.pkl.gz for tpgate.py/tpcheck.py from the pinned sqlglot checkout ($1).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(mktemp)
cd "$1"
export PYTHONPATH="$1" PYTHONHASHSEED=0
python "$HERE/tpc_build.py" "$1" "$OUT" 200 "$HERE/../tpc_full.pkl"
python "$HERE/tpc_lines.py" "$1" "$OUT"
python "$HERE/tpc_runtime.py" "$1" "$OUT"
python -c "import gzip,shutil,sys; shutil.copyfileobj(open(sys.argv[1],'rb'), gzip.open(sys.argv[2],'wb',9))" "$OUT" "$HERE/../tpc.pkl.gz"
rm "$OUT"
