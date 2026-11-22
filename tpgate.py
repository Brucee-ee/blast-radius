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


_srv = {}


def _server(checker, data_path):
    """The check server (tpcheck.py --serve), started once on the pristine scratch copy: it has
    the sqlglot package imported, so a check of a patch that leaves those modules alone skips
    the interpreter start and most of the imports."""
    if "p" not in _srv:
        _srv["p"] = None
        try:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", BR_TP_EXEC="")
            p = subprocess.Popen([sys.executable, "-S", str(checker), "--serve", str(SCRATCH), str(data_path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=env, text=True)
            line = p.stdout.readline()
            if line.startswith("READY "):
                _srv["p"] = p
                import atexit
                atexit.register(_stop, p)
                _srv["mods"] = set(line[6:].split(","))
                _srv["mods"] = {x.strip() for x in _srv["mods"]}
            else:
                p.kill()
        except Exception:  # noqa: BLE001
            _srv["p"] = None
    return _srv["p"]


def _stop(p):
    try:
        p.stdin.close()
        p.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def _ask(p, exec_only, only):
    try:
        p.stdin.write(("1" if exec_only else "0") + (" " + ",".join(map(str, sorted(only))) if only else "") + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        if line.startswith("RC "):
            return int(line[3:])
    except Exception:  # noqa: BLE001
        pass
    _srv["p"] = None   # broken: back to one process per check
    return None


def import_check(files, src, apply_hunks, module, checker, data_path, timeout=60):
    """True when `module` fails to import (after `import sqlglot`) with the patch applied to the
    scratch copy.  Uses the check server when it has none of the patched modules loaded."""
    _ensure_base(src)
    server = _server(checker, data_path)
    touched = []
    try:
        paths = []
        for fp in files:
            path = fp.new if not fp.is_deleted else fp.old
            target = SCRATCH / path
            orig = src / path
            if fp.is_deleted or not orig.exists():
                return False
            new_lines = apply_hunks(orig.read_text(encoding="utf-8").split("\n"), fp)
            if new_lines is None:
                return False
            touched.append((target, orig))
            target.write_text("\n".join(new_lines), encoding="utf-8")
            paths.append(path)
        rc = None
        if server is not None and module not in _srv["mods"] and not any(
                path.endswith(".py") and _module(path) in _srv["mods"] for path in paths):
            try:
                server.stdin.write("I " + module + "\n")
                server.stdin.flush()
                line = server.stdout.readline()
                if line.startswith("RC "):
                    rc = int(line[3:])
            except Exception:  # noqa: BLE001
                _srv["p"] = None
        if rc is None:
            code = ("import sys; sys.path.insert(0, %r); import importlib\n"
                    "import sqlglot\nimportlib.import_module(%r)") % (str(SCRATCH), module)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
            try:
                rc = subprocess.run([sys.executable, "-S", "-c", code], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 0
        return rc != 0
    finally:
        for target, orig in touched:
            try:
                shutil.copyfile(orig, target)
                st = orig.stat()
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass


def unchanged(files, src, apply_hunks, data_path, checker, only=None, timeout=60, mods=None):
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
    server = _server(checker, data_path)
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
        exec_only = all(p.startswith("sqlglot/executor/") and p != "sqlglot/executor/__init__.py"
                        for _, p in edits)
        rc = None
        if server is not None and not any(_module(path) in _srv["mods"] for _, path in edits):
            rc = _ask(server, exec_only, only)
        if rc is None:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", BR_TP_EXEC="1" if exec_only else "")
            rc = subprocess.run(
                [sys.executable, "-S", str(checker), str(SCRATCH), str(data_path)]
                + ([",".join(map(str, sorted(only)))] if only else []),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=timeout,
            ).returncode
        ok = rc == 0
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
