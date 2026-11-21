"""Build-time: write the TPC-DS check's data pickle (argv[2]): queries, schema, sampled tables,
the pinned sqlglot's fingerprints (tpcheck.py dump), the hash of its DuckDB SQL per query and of
its full-data result (columns and rows, in order) per executed query.  The full tables go to
argv[4] for tpcheck.py's full-data rerun."""
import hashlib, pickle, subprocess, sys, os
root, out, n, full_out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
sys.path.insert(0, root)
import tests.test_executor as te
from tests.helpers import load_sql_fixture_pairs, TPCDS_SCHEMA
from sqlglot import find_tables, parse_one
from sqlglot.executor import execute
te.TestExecutor.setUpClass()
C = te.TestExecutor
full = {name: (tuple(t.columns), [tuple(r) for r in t.rows]) for name, t in C.tpcds_tables.items()}
tables = {name: (cols, rows[:n]) for name, (cols, rows) in full.items()}
pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
queries = [(bool(meta.get("execute")), sql) for meta, sql, _ in pairs]
data = {"tables": tables, "schema": TPCDS_SCHEMA, "queries": queries, "fp": []}
pickle.dump(data, open(out, "wb"))
pickle.dump(full, open(full_out, "wb"), protocol=5)
here = os.path.dirname(os.path.abspath(__file__))
lines = subprocess.run([sys.executable, os.path.join(here, "..", "tpcheck.py"), root, out, "dump"],
                       capture_output=True, text=True, check=True).stdout.split("\n")
lines = [l.split() for l in lines if l.strip()]
data["mods"] = lines.pop()[1].split(",")  # sqlglot modules the check's pipeline imports
assert len(lines) == len(queries), (len(lines), len(queries))
data["fp"] = [l[0] for l in lines]
data["duck"] = [l[1] if len(l) > 1 else "" for l in lines]
import time
results, costs = [], []
for execute_flag, sql in queries:
    if not execute_flag:
        results.append("")
        costs.append(0.0)
        continue
    t0 = time.process_time()
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    r = execute(e, schema=TPCDS_SCHEMA, tables={t.name: C.tpcds_tables[t.name] for t in find_tables(e)})
    costs.append(time.process_time() - t0)
    results.append(hashlib.sha1("\x00".join((repr(r.columns), repr(r.rows))).encode("utf-8", "replace")).hexdigest())
data["full"] = results
data["full_cost"] = costs
pickle.dump(data, open(out, "wb"))
print("queries", len(queries), "executed", sum(q[0] for q in queries), "rows", sum(len(r) for _, r in tables.values()), "size", os.path.getsize(out))
