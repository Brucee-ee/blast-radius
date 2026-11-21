"""Build-time: which sqlglot lines each TPC-DS query of test_execute_tpcds runs.

Writes into data pickle (argv[2]): "qlines" {file: {line: bitmask of query indices}} covering
the test's own pipeline on the full data and tpcheck.py's pipeline on the sampled data, and
"rt" {file: [lines]} for lines the Python executor runs outside code generation on the full
data (a commit touching those always keeps the test)."""
import pickle, sys
import coverage
root, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
cov = coverage.Coverage(data_file=None, source=[root + "/sqlglot"])
cov.start()
import tests.test_executor as te
from sqlglot import find_tables, parse_one, transpile
from sqlglot.executor import execute
from sqlglot.executor.table import Table
te.TestExecutor.setUpClass()
C = te.TestExecutor
te.initializer(te.TPCDS_SCHEMA, C.tpcds_tables)
data = pickle.load(open(out, "rb"))
small = {n: Table(columns=c, rows=r) for n, (c, r) in data["tables"].items()}
for i, (meta, sql, _) in enumerate(C.tpcds_sqls):
    cov.switch_context("q%d" % i)
    try:
        e = pickle.loads(pickle.dumps(parse_one(sql)))
        if meta.get("execute"):
            te.mp_execute(e, meta)
            transpile(sql, write="duckdb")
            execute(pickle.loads(pickle.dumps(e)), schema=data["schema"], tables={t.name: small[t.name] for t in find_tables(e)})
    except Exception as err:
        print("query", i, "error", err)
cov.stop()
d = cov.get_data()
q = {}
for f in d.measured_files():
    rel = f[len(root) + 1:]
    for line, ctxs in (d.contexts_by_lineno(f) or {}).items():
        mask = 0
        for c in ctxs:
            if c.startswith("q"):
                mask |= 1 << int(c[1:])
        if mask:
            q.setdefault(rel, {})[line] = mask
data["qlines"] = q
pickle.dump(data, open(out, "wb"))
print("files", len(q), "lines", sum(len(v) for v in q.values()))
