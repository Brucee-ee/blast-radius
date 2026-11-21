"""Build-time: lines (outside sqlglot/executor) the Python executor runs on the full TPC-DS data
while not generating code; stored as "rt" in the data pickle given as argv[2]."""
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
for meta, sql, _ in C.tpcds_sqls:
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    te.mp_execute(e, meta)
d = cov.get_data()
out = {}
for f in d.measured_files():
    rel = f[len(root)+1:]
    if rel.startswith('sqlglot/executor/'): continue
    ls = sorted(d.lines(f) or [])
    if ls: out[rel] = ls
d0 = pickle.load(open(sys.argv[2], "rb")); d0["rt"] = out; pickle.dump(d0, open(sys.argv[2], "wb"))
print("runtime lines", sum(len(v) for v in out.values()))
