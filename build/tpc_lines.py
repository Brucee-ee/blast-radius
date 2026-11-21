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
import sqlglot.executor as ex
real_optimize = ex.optimize
cur = [None]


def front_optimize(*a, **k):
    # lines the optimizer runs are also recorded as the query's "front end" (f<i>)
    cov.switch_context("f%d" % cur[0])
    try:
        return real_optimize(*a, **k)
    finally:
        cov.switch_context("q%d" % cur[0])


ex.optimize = front_optimize
for i, (meta, sql, _) in enumerate(C.tpcds_sqls):
    cur[0] = i
    cov.switch_context("f%d" % i)
    try:
        e = pickle.loads(pickle.dumps(parse_one(sql)))
        cov.switch_context("q%d" % i)
        if meta.get("execute"):
            te.mp_execute(e, meta)
            transpile(sql, write="duckdb")
            execute(pickle.loads(pickle.dumps(e)), schema=data["schema"], tables={t.name: small[t.name] for t in find_tables(e)})
    except Exception as err:
        print("query", i, "error", err)
cov.stop()
d = cov.get_data()
q = {}
front = {}  # lines run while parsing a query or optimizing it
for f in d.measured_files():
    rel = f[len(root) + 1:]
    for line, ctxs in (d.contexts_by_lineno(f) or {}).items():
        mask = 0
        for c in ctxs:
            if c[:1] in ("q", "f"):
                mask |= 1 << int(c[1:])
            if c[:1] == "f":
                front.setdefault(rel, set()).add(line)
        if mask:
            q.setdefault(rel, {})[line] = mask
data["qlines"] = q
data["front"] = {f: sorted(v) for f, v in front.items()}
# modules the check never imports but this full-data run does count as imported too
data["mods"] = sorted(set(data.get("mods", ())) | {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")})
pickle.dump(data, open(out, "wb"))
print("files", len(q), "lines", sum(len(v) for v in q.values()))
