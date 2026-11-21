"""Decide at run time whether a commit can change test_execute_tpcds.

The commit's patch is applied to a scratch copy of the pinned sqlglot in /tmp
and tpcheck.py reruns the test's pipeline on it.  Any doubt keeps the test.
"""
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRATCH = Path("/tmp/brtp")


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
            # an edit to a def line (name, parameters, defaults) only matters to its callers
            inner = [r for r in ranges if r[0] <= lo and hi <= r[2]]
            if not inner:
                return None
            _, a, b = max(inner)
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
        if path.startswith("sqlglot/executor/"):
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


def unchanged(files, src, apply_hunks, data_path, checker, only=None, timeout=60):
    """True only when the patch provably leaves the TPC-DS pipeline's outputs alone."""
    edits = _edits(files)
    if not edits:
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
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
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
