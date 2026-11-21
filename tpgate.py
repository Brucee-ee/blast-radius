"""Decide at run time whether a commit can change test_execute_tpcds.

The commit's patch is applied to a scratch copy of the pinned sqlglot in /tmp
and tpcheck.py reruns the test's pipeline on it.  Any doubt keeps the test.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRATCH = Path("/tmp/brtp")


_KEEP_TESTS = ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py")


def _ignorable(path):
    """Files the TPC-DS test never reads: other test modules, fixtures other than the TPC-H and
    TPC-DS data its class loads, docs."""
    if path.startswith("tests/"):
        if path in _KEEP_TESTS or "conftest" in path or path.startswith(("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/")):
            return False
        return path.startswith("tests/fixtures/") or (
            path.endswith(".py") and os.path.basename(path).startswith("test_"))
    return path.startswith("docs/") or path.endswith((".md", ".rst"))


TPCDS_SQL = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
TPCH_SQL = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"


def _pairs(text):
    """(meta, sql) of each query, as tests/helpers.py load_sql_fixture_pairs reads them; the
    expected optimized SQL that follows each query is not used by test_execute_tpcds."""
    statements = "\n".join(l for l in text.splitlines() if l and not l.startswith("--")).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            lines = statements[i].strip().split("\n")
            meta = []
            j = 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta.append((key.lstrip("#").strip(), val.strip()))
                j += 1
            out.append((meta, "\n".join(lines[j:])))
    return out


def _same_queries(fp, apply_hunks, fixture):
    try:
        old = fixture.read_text(encoding="utf-8")
        new = apply_hunks(old.split("\n"), fp)
        return new is not None and _pairs("\n".join(new)) == _pairs(old)
    except Exception:  # noqa: BLE001
        return False


def relevant(files, apply_hunks=None, fixture=None):
    """The patch's files minus those the test cannot see: other test modules and fixtures,
    docs, an edit of tpc-h.sql (setUpClass only splits it), and an edit of tpc-ds.sql that
    leaves every query and its meta as they were (only the expected outputs changed)."""
    out = []
    for fp in files:
        plain = not (fp.binary or fp.is_new or fp.is_deleted) and fp.old == fp.new
        if fp.binary or not all(p is None or _ignorable(p) for p in (fp.old, fp.new)):
            if plain and fp.old == TPCH_SQL:
                continue
            if plain and fp.old == TPCDS_SQL and apply_hunks and fixture and _same_queries(fp, apply_hunks, fixture):
                continue
            out.append(fp)
    return out


def _ensure_base(src):
    dst = SCRATCH / "sqlglot"
    if not dst.exists():
        SCRATCH.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src / "sqlglot", dst, ignore=shutil.ignore_patterns("*.pyi", "py.typed"))
        for d, _, fs in os.walk(dst):  # the copy keeps the read-only image's modes
            os.chmod(d, 0o755)
            for f in fs:
                os.chmod(os.path.join(d, f), 0o644)
    return dst


def touches(files, chunks, runtime_lines):
    for fp in files:
        lines = runtime_lines.get(fp.old or "", ())
        if not lines:
            continue
        lines = set(lines)
        for rem, add, pos in chunks(fp):
            near = {no for no, _ in rem}
            if pos is not None:
                near |= {pos, pos + 1}
            if near & lines:
                return True
    return False


def _func_ranges(text):
    """(header_start, body_start, end) of every function; the header is the def line(s)."""
    out = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, node.body[0].lineno, node.end_lineno))
    return out


def _def_names(lines):
    return sorted(m.group(1) for m in (re.match(r"\s*(?:async\s+)?def\s+(\w+)", t) for t in lines) if m)


def queries_reached(files, src, chunks, qlines):
    """Bitmask of TPC-DS queries that run a function the patch edits, or None when an edit is
    outside a function body (module or class level, decorators, signatures, new or deleted
    files), where every query has to be checked."""
    mask = 0
    for fp in files:
        if fp.is_new or fp.is_deleted or not fp.old:
            return None
        text = (src / fp.old).read_text(encoding="utf-8")
        ranges = _func_ranges(text)
        ql = qlines.get(fp.old, {})
        for rem, add, pos in chunks(fp):
            if rem:
                lo, hi = rem[0][0], rem[-1][0]
            else:
                lo, hi = pos, pos + 1
            # an edit to a def line (parameters, defaults) only matters to its callers
            inner = [r for r in ranges if r[0] <= lo and hi <= r[2]]
            if not inner:
                return None
            head, a, b = max(inner)
            if any(no == head for no, _ in rem) and _def_names(t for _, t in rem) != _def_names(add):
                return None  # a renamed function can shadow another one: check every query
            for line in range(a, b + 1):
                mask |= ql.get(line, 0)
    return mask


def front_free(files, src, chunks, front):
    """True when every edit is inside a function that never ran while the TPC-DS queries were
    parsed or optimized: the optimizer's output is then the pinned one."""
    for fp in files:
        path = fp.old
        if fp.is_new or fp.is_deleted or not path or path == "sqlglot/executor/__init__.py" or fp.new != path:
            return False
        ranges = _func_ranges((src / path).read_text(encoding="utf-8"))
        lines = set(front.get(path, ()))
        for rem, add, pos in chunks(fp):
            lo, hi = (rem[0][0], rem[-1][0]) if rem else (pos, pos + 1)
            inner = [r for r in ranges if r[0] <= lo and hi <= r[2]]
            if not inner:
                return False
            head, _, end = max(inner)
            if any(no == head for no, _ in rem) and _def_names(t for _, t in rem) != _def_names(add):
                return False
            if lines.intersection(range(head, end + 1)):
                return False
    return True


def _edits(files):
    edits = []
    for fp in files:
        path = fp.new if not fp.is_deleted else fp.old
        if fp.binary or not path:
            return None
        if fp.old and fp.new and fp.old != fp.new and not fp.is_new:
            return None  # rename
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            return None
        edits.append((fp, path))
    return edits or None


def applies(files, src, apply_hunks):
    """For a patch no TPC-DS query can reach: still require it to be a plain sqlglot edit."""
    edits = _edits(files)
    if not edits:
        return False
    for fp, path in edits:
        if fp.is_new or fp.is_deleted:
            return False
        orig = src / path
        if not orig.exists() or apply_hunks(orig.read_text(encoding="utf-8").split("\n"), fp) is None:
            return False
    return True


def _module(path):
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def unchanged(files, src, apply_hunks, data_path, checker, only=None, timeout=60, mods=None, fast=False):
    """True only when the patch provably leaves the TPC-DS pipeline's outputs alone."""
    edits = _edits(files)
    if not edits:
        return False
    if mods is not None and all(_module(path) not in mods for _, path in edits):
        # the pipeline never imports the patched modules, so the check could only agree
        try:
            return all(fp.is_new or fp.is_deleted or apply_hunks(
                (src / path).read_text(encoding="utf-8").split("\n"), fp) is not None for fp, path in edits)
        except OSError:
            return False
    try:
        base = _ensure_base(src)
    except OSError:
        return False
    touched = []
    ok = False
    try:
        for fp, path in edits:
            target = SCRATCH / path
            orig = src / path
            if fp.is_deleted:
                touched.append((target, orig))
                target.unlink()
                continue
            if fp.is_new:
                old_lines = []
            else:
                if not orig.exists():
                    return False
                old_lines = orig.read_text(encoding="utf-8").split("\n")
            new_lines = apply_hunks(old_lines, fp)
            if new_lines is None:
                return False
            touched.append((target, orig))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(new_lines), encoding="utf-8")
        # the executor's own modules (bar executor/__init__.py, which calls the optimizer) are
        # never used while parsing or optimizing either
        fast = fast or all(p.startswith("sqlglot/executor/") and p != "sqlglot/executor/__init__.py"
                           for _, p in edits)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", BR_TP_EXEC="1" if fast else "")
        r = subprocess.run(
            [sys.executable, "-S", str(checker), str(SCRATCH), str(data_path)]
            + ([",".join(map(str, sorted(only)))] if only else []),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=timeout,
        )
        ok = r.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    finally:
        for target, orig in touched:
            try:
                if orig.exists():
                    shutil.copyfile(orig, target)
                    st = orig.stat()
                    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # keeps its .pyc valid
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
    return ok
