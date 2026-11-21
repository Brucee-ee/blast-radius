"""Build-time: write data/tpc.pkl (queries, schema, sampled tables) and its baseline fingerprints."""
import pickle, subprocess, sys, os
root, out, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, root)
import tests.test_executor as te
from tests.helpers import load_sql_fixture_pairs, TPCDS_SCHEMA
te.TestExecutor.setUpClass()
C = te.TestExecutor
tables = {name: (tuple(t.columns), [tuple(r) for r in t.rows[:n]]) for name, t in C.tpcds_tables.items()}
queries = [(bool(meta.get("execute")), sql) for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
data = {"tables": tables, "schema": TPCDS_SCHEMA, "queries": queries, "fp": []}
pickle.dump(data, open(out, "wb"))
here = os.path.dirname(os.path.abspath(__file__))
fp = subprocess.run([sys.executable, os.path.join(here, "..", "tpcheck.py"), root, out, "dump"], capture_output=True, text=True, check=True).stdout.split()
assert len(fp) == len(queries), (len(fp), len(queries))
data["fp"] = fp
pickle.dump(data, open(out, "wb"))
print("queries", len(queries), "executed", sum(q[0] for q in queries), "rows", sum(len(r) for _, r in tables.values()), "size", os.path.getsize(out))
