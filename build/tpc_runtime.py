"""Build-time: lines (outside sqlglot/executor) the Python executor runs on the full TPC-DS data
while not generating code, minus those it also runs on the sampled tables tpcheck.py executes
(whose outcome tpcheck.py therefore sees); stored as "rt" in the data pickle given as argv[2]."""
import sys, coverage, pickle
root = sys.argv[1]; sys.path.insert(0, root)
cov = coverage.Coverage(data_file=None, source=[root + '/sqlglot'])
import tests.test_executor as te
from sqlglot import parse_one
from sqlglot.executor import python as pyexec
te.TestExecutor.setUpClass()
C = te.TestExecutor
te.initializer(te.TPCDS_SCHEMA, C.tpcds_tables)
real_exec = pyexec.PythonExecutor.execute
real_gen = pyexec.PythonExecutor.generate
depth = [0]
def ex(self, plan, outer_scope=None):
    depth[0] += 1
    if depth[0] == 1: cov.start()
    try: return real_exec(self, plan, outer_scope)
    finally:
        depth[0] -= 1
        if depth[0] == 0: cov.stop()
def gen(self, e):
    if depth[0]: cov.stop()
    try: return real_gen(self, e)
    finally:
        if depth[0]: cov.start()
pyexec.PythonExecutor.execute = ex; pyexec.PythonExecutor.generate = gen
def lines_run(tables):
    cov.erase()
    for meta, sql, _ in C.tpcds_sqls:
        e = pickle.loads(pickle.dumps(parse_one(sql)))
        if meta.get("execute"):
            execute(e, schema=te.TPCDS_SCHEMA, tables={t.name: tables[t.name] for t in find_tables(e)})
    d = cov.get_data()
    return {f[len(root)+1:]: set(d.lines(f) or []) for f in d.measured_files()}
from sqlglot import find_tables
from sqlglot.executor import execute
from sqlglot.executor.table import Table
d0 = pickle.load(open(sys.argv[2], "rb"))
full = lines_run(C.tpcds_tables)
small = lines_run({n: Table(columns=c, rows=r) for n, (c, r) in d0["tables"].items()})
out = {}
for rel, ls in full.items():
    if rel.startswith('sqlglot/executor/'): continue
    ls = sorted(ls - small.get(rel, set()))
    if ls: out[rel] = ls
d0["rt"] = out; pickle.dump(d0, open(sys.argv[2], "wb"))
print("runtime lines", sum(len(v) for v in out.values()))
