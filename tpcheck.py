"""Differential check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

Run as: python tpcheck.py <sqlglot_root> <data.pkl> [query indices | dump]

Imports sqlglot from <sqlglot_root> (a copy with the commit's patch applied) and reruns the
TPC-DS pipeline the test runs on every query: parse, pickle round trip, optimize, plan, Python
code generation, execution on a small sample of every table, and the DuckDB transpilation the
test's expected result comes from.  A fingerprint of all of it is compared with the one recorded
from the pinned sqlglot at build time.

A query whose fingerprint differs only in what the executor produced on the way (optimized SQL,
plan, generated code, sample rows), with the same DuckDB SQL and no error, is then executed on
the full TPC-DS tables, as the test does; if its columns and rows, in order, equal the pinned
sqlglot's, the test's comparison for that query cannot change.  Exit 0 when every query is
settled that way, 1 otherwise (or on any error).  "dump" prints the fingerprints (build time).
"""
import hashlib
import os
import pickle
import sys

MAX_FULL = 64  # queries rerun on the full tables before giving up
CPU_LIMIT = 12  # seconds; a check that needs more (say, a patch that slows the executor down a lot)
                # is killed by the kernel and the test is kept


def _h(*parts):
    return hashlib.sha1("\x00".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()


def main():
    root, data_path = sys.argv[1], sys.argv[2]
    arg = sys.argv[3] if len(sys.argv) > 3 else ""
    dump = arg == "dump"
    only = None if dump or not arg else {int(x) for x in arg.split(",")}
    if not dump:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT, CPU_LIMIT + 1))
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

    current = [None]
    if os.environ.get("BR_TP_EXEC") == "1":
        # every edit is in a function the queries' parsing and optimization never ran: the
        # optimizer's output is the pinned one, stored at build time
        def stored_optimize(*a, **k):
            return pickle.loads(data["opt"][current[0]])

        ex.optimize = stored_optimize

    tables = {name: Table(columns=cols, rows=rows) for name, (cols, rows) in data["tables"].items()}
    schema = data["schema"]
    full = []
    reruns = 0

    def same_on_full_tables(i, sql):
        if not full:
            with open(os.path.join(os.path.dirname(data_path), "tpc_full.pkl"), "rb") as f:
                full.append({name: Table(columns=c, rows=r) for name, (c, r) in pickle.load(f).items()})
        e = pickle.loads(pickle.dumps(parse_one(sql)))
        r = execute(e, schema=schema, tables={t.name: full[0][t.name] for t in find_tables(e)})
        return _h(repr(r.columns), repr(r.rows)) == data["full"][i]

    order = range(len(data["queries"]))
    if not dump:  # cheapest full-data rerun first, so a real difference is found early
        order = sorted(order, key=lambda i: data["full_cost"][i])
    out = []
    for i in order:
        execute_flag, sql = data["queries"][i]
        if only is not None and i not in only:
            continue
        rec.clear()
        current[0] = i
        duck = err = None
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            rec.append("parsed")
            if execute_flag:
                used = {t.name: tables[t.name] for t in find_tables(e)}
                r = execute(e, schema=schema, tables=used)
                rec.append(repr(r.columns))
                rec.append(repr(sorted(map(repr, r.rows))))
                duck = transpile(sql, write="duckdb")[0]
                rec.append(duck)
        except BaseException as exc:  # noqa: BLE001
            err = "ERR " + type(exc).__name__
            rec.append(err)
        h = hashlib.sha1("\x00".join(sorted(map(str, rec))).encode("utf-8", "replace")).hexdigest()
        if dump:
            out.append((h, _h(duck) if execute_flag and not err else ""))
        elif h != data["fp"][i]:
            if not execute_flag or err or _h(duck) != data["duck"][i] or reruns >= MAX_FULL:
                sys.exit(1)
            reruns += 1
            try:
                if not same_on_full_tables(i, sql):
                    sys.exit(1)
            except Exception:  # noqa: BLE001
                sys.exit(1)
    if dump:
        mods = sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))
        sys.stdout.write("\n".join("%s %s" % x if x[1] else x[0] for x in out) + "\nMODS " + ",".join(mods) + "\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
