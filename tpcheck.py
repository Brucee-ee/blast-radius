"""Differential check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

Run as: python tpcheck.py <sqlglot_root> <data.pkl> [dump]

Imports sqlglot from <sqlglot_root> (a copy with the commit's patch applied),
reruns the TPC-DS pipeline the test runs (parse, pickle round trip, optimize,
plan, Python code generation, execution on a small sample of every table,
DuckDB transpilation) and compares a fingerprint of each step with the one
recorded from the pinned sqlglot at build time.  Exit 0 when everything is
identical, 1 at the first difference (or any error).  With "dump" it prints
the fingerprints instead (used at build time).
"""
import hashlib
import pickle
import sys


def main():
    root, data_path = sys.argv[1], sys.argv[2]
    arg = sys.argv[3] if len(sys.argv) > 3 else ""
    dump = arg == "dump"
    only = None if dump or not arg else {int(x) for x in arg.split(",")}
    sys.path.insert(0, root)
    with open(data_path, "rb") as f:
        data = pickle.load(f)

    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor import python as pyexec
    from sqlglot.executor.table import Table
    import sqlglot.executor as ex

    rec = []
    real_compile = compile

    def rec_compile(src, *a, **k):
        rec.append(src)
        return real_compile(src, *a, **k)

    pyexec.compile = rec_compile
    real_plan = ex.Plan

    def rec_plan(expression):
        rec.append(expression.sql())
        return real_plan(expression)

    ex.Plan = rec_plan

    tables = {name: Table(columns=cols, rows=rows) for name, (cols, rows) in data["tables"].items()}
    schema = data["schema"]
    out = []
    for i, (execute_flag, sql) in enumerate(data["queries"]):
        if only is not None and i not in only:
            continue
        rec.clear()
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            rec.append("parsed")
            if execute_flag:
                used = {t.name: tables[t.name] for t in find_tables(e)}
                r = execute(e, schema=schema, tables=used)
                rec.append(repr(r.columns))
                rec.append(repr(sorted(map(repr, r.rows))))
                rec.append(transpile(sql, write="duckdb")[0])
        except BaseException as err:  # noqa: BLE001
            rec.append("ERR " + type(err).__name__)
        h = hashlib.sha1("\x00".join(sorted(map(str, rec))).encode("utf-8", "replace")).hexdigest()
        if dump:
            out.append(h)
        elif h != data["fp"][i]:
            sys.exit(1)
    if dump:
        sys.stdout.write("\n".join(out) + "\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
