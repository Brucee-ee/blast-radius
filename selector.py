"""Blast Radius selector.

For every patch, decide which sqlglot tests could fail and split them over the
fewest-cost set of workers.  Decisions come from maps built once from the pinned
sqlglot commit (per-test line coverage, which classes each test instantiates,
which fixture files each test reads) plus an AST analysis of the patch.  Nothing
here looks up a patch, or a kind of edit, in a table of answers.

When the analysis cannot place a change it falls back to a wider set of tests,
up to all of them, because a miss costs 300 s and a full run costs about 40 s.
"""

import ast
import json
import operator
import re
import sys
from pathlib import Path

import os
DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
SRC = APP / "src"
if not SRC.exists() and (APP / "src.tgz").exists():          # running from a checkout
    import tarfile, tempfile
    _tmp = Path(tempfile.mkdtemp())
    tarfile.open(APP / "src.tgz").extractall(_tmp, filter="data")
    SRC = _tmp
WORKER_COST = 2.0
MAX_WORKERS = 8
ALL = "ALL"
IMPORT_CTX = -1

DOC_EXT = {".md", ".rst", ".txt", ".png", ".svg", ".jpg", ".gif"}


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- map


def load_data(name):
    """data/<name>.marshal (made by the Dockerfile from the .json, quicker to load), data/<name>.json,
    or data/<name>.json.gz when run from a checkout."""
    p = APP / "data" / (name + ".marshal")
    if p.exists():
        import marshal
        return marshal.loads(p.read_bytes())
    p = APP / "data" / (name + ".json")
    if p.exists():
        return json.loads(p.read_text())
    import gzip
    with gzip.open(APP / "data" / (name + ".json.gz"), "rt") as f:
        return json.load(f)


class Map:
    def __init__(self, tests):
        m = load_data("map")
        num = {t["id"]: t["number"] for t in tests}
        self.num = num
        self.tests = tests
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.id_to_num = [num.get(i) for i in m["ids"]]
        self.sets = m["sets"]
        self.files = {f: {int(k): v for k, v in d.items()} for f, d in m["files"].items()}
        self.files_imp = {f: {int(k): v for k, v in d.items()} for f, d in m.get("files_imp", {}).items()}
        self.classes = m["classes"]
        self.opens = m["opens"]
        self.refs = m["refs"]
        self._set_cache = {}
        self.all = set(t["number"] for t in tests)
        self.attr = m.get("attr", {})
        self.attr_fn = m.get("attr_fn", {})   # name -> reading function -> owner classes
        self.body_refs = m.get("body_refs", {})
        self.mro = m.get("mro", {})
        self.creation_names = set(m.get("creation_names", ()))
        self.hooks = m.get("hooks", [])
        self._cmp = None
        self.components = m.get("components", {})
        self.subs = {}          # class key -> owner keys that have it in their MRO
        for o, chain in self.mro.items():
            for c in chain:
                self.subs.setdefault(c, set()).add(o)

    def _load_cmp(self):
        if self._cmp is None:
            self._cmp = load_data("cmp")
        return self._cmp

    @property
    def cmp_sites(self):
        c = self._load_cmp()
        if not isinstance(c.get("cmp_sites"), set):
            c["cmp_sites"] = set(c.get("cmp_sites", ()))
        return c["cmp_sites"]

    @property
    def cmp(self):
        return self._load_cmp().get("cmp", {})

    @property
    def cmp_import(self):
        return self._load_cmp().get("cmp_import", {})

    def conv(self, idxs):
        out = set()
        for i in idxs:
            if i < 0:
                continue
            n = self.id_to_num[i]
            if n is not None:
                out.add(n)
        return out

    def set_tests(self, sid):
        r = self._set_cache.get(sid)
        if r is None:
            r = self._set_cache[sid] = self.conv(self.sets[sid])
        return r

    def lines(self, path, start, end, imp=False):
        """Tests that ran lines start..end.  With imp, also tests during which the module was
        imported (needed for import-time code: module level, class bodies, def lines)."""
        out = set()
        for d in ((self.files.get(path), self.files_imp.get(path)) if imp else (self.files.get(path),)):
            if not d:
                continue
            for ln in range(start, end + 1):
                sid = d.get(ln)
                if sid is not None:
                    out |= self.set_tests(sid)
        return out

    def import_ran(self, path, start, end):
        """Did any of these lines run while a module was being imported (or before tests ran)?"""
        di = self.files_imp.get(path) or {}
        d = self.files.get(path) or {}
        for ln in range(start, end + 1):
            if ln in di:
                return True
            sid = d.get(ln)
            if sid is not None and IMPORT_CTX in self.sets[sid]:
                return True
        return False

    def has_import_ctx(self, path, start, end):
        d = self.files.get(path)
        if not d:
            return False
        for ln in range(start, end + 1):
            sid = d.get(ln)
            if sid is not None and IMPORT_CTX in self.sets[sid]:
                return True
        return False

    def file_union(self, path):
        out = set()
        for d in (self.files.get(path) or {}, self.files_imp.get(path) or {}):
            for sid in set(d.values()):
                out |= self.set_tests(sid)
        return out

    def class_tests(self, key):
        return self.conv(self.classes.get(key, ()))


# ------------------------------------------------------------------------ patch


class FilePatch:
    def __init__(self):
        self.old = None
        self.new = None
        self.is_new = False
        self.is_deleted = False
        self.binary = False
        self.hunks = []  # (old_start, old_count, new_start, new_count, lines)


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            hunk = None
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            continue
        if cur is None:
            if raw.startswith("--- "):
                cur = FilePatch()
                files.append(cur)
            else:
                continue
        if hunk is None or not raw.startswith(("+", "-", " ", "\\")):
            if raw.startswith("@@"):
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
                if m:
                    hunk = [int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1), []]
                    cur.hunks.append(hunk)
                continue
            hunk = None
            if raw.startswith("new file mode"):
                cur.is_new = True
            elif raw.startswith("deleted file mode"):
                cur.is_deleted = True
            elif raw.startswith("rename from "):
                cur.old = raw[len("rename from "):]
            elif raw.startswith("rename to "):
                cur.new = raw[len("rename to "):]
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur.binary = True
            elif raw.startswith("--- "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    cur.is_new = True
                elif cur.old is None:
                    cur.old = p[2:] if p.startswith("a/") else p
            elif raw.startswith("+++ "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    cur.is_deleted = True
                elif cur.new is None:
                    cur.new = p[2:] if p.startswith("b/") else p
            continue
        hunk[4].append(raw)
    return files


def chunks(fp):
    """Yield (removed_old_lines[(no,text)], added[(text)], insert_after_old_line)."""
    out = []
    for os_, oc, ns, nc, lines in fp.hunks:
        o = os_
        rem, add, pos = [], [], None
        for ln in lines + [" "]:
            c = ln[:1]
            if c == "-":
                rem.append((o, ln[1:]))
                o += 1
                if pos is None:
                    pos = o - 2
            elif c == "+":
                add.append(ln[1:])
                if pos is None:
                    pos = o - 1
            elif c == "\\":
                continue
            else:
                if rem or add:
                    out.append((rem, add, pos))
                rem, add, pos = [], [], None
                o += 1
    return out


def apply_hunks(old_lines, fp):
    out = []
    i = 0  # index into old_lines
    for os_, oc, ns, nc, lines in fp.hunks:
        start = max(os_ - 1, 0) if oc else os_
        if start < i:
            return None
        out.extend(old_lines[i:start])
        i = start
        for ln in lines:
            c = ln[:1]
            if c == "\\":
                continue
            body = ln[1:]
            if c == " ":
                if i >= len(old_lines) or old_lines[i] != body:
                    return None
                out.append(body)
                i += 1
            elif c == "-":
                if i >= len(old_lines) or old_lines[i] != body:
                    return None
                i += 1
            elif c == "+":
                out.append(body)
    out.extend(old_lines[i:])
    return out


# -------------------------------------------------------------------------- AST

COMPOUND = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While,
            ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "TryStar"):
    COMPOUND += (ast.TryStar,)
if hasattr(ast, "Match"):
    COMPOUND += (ast.Match,)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def strip_docstrings(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                n.body = b[1:] or [ast.Pass()]
    return tree


def has_future_annotations(tree):
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
               for n in tree.body)


def strip_types(tree):
    """Drop what `from __future__ import annotations` keeps from ever being evaluated: annotations of
    function parameters and returns, of module-level and local variables, and `if TYPE_CHECKING:`
    blocks.  Class-level annotations stay (dataclasses and the like read them)."""
    def walk(node, in_class):
        for f, v in ast.iter_fields(node):
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, ast.AST):
                        walk(x, isinstance(node, ast.ClassDef) and f == "body")
            elif isinstance(v, ast.AST):
                walk(v, False)
        if isinstance(node, ast.arg):
            node.annotation = None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
        elif isinstance(node, ast.AnnAssign) and not in_class:
            node.annotation = ast.Constant(value=None)
        elif isinstance(node, ast.If) and isinstance(node.test, (ast.Name, ast.Attribute)) \
                and getattr(node.test, "id", getattr(node.test, "attr", None)) == "TYPE_CHECKING":
            node.body = [ast.Pass()]
    walk(tree, False)
    return tree


def types_only_change(old_text, new_text):
    """True when two versions of a module differ only in what strip_types drops (and docstrings,
    comments, layout), in a module that does not evaluate its annotations."""
    try:
        a = ast.parse(old_text)
        b = ast.parse(new_text)
    except SyntaxError:
        return False
    if not (has_future_annotations(a) and has_future_annotations(b)):
        return False
    _drop_added_stdlib_imports(a, b)
    return _canon(a) == _canon(b)


def _canon(tree):
    tree = strip_docstrings(strip_types(tree))
    # a run of consecutive undecorated module-level functions only binds names: order is moot
    body, run = [], []
    for n in tree.body + [None]:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list:
            run.append(n)
            continue
        if len({f.name for f in run}) == len(run):
            run.sort(key=lambda f: f.name)
        body += run
        run = []
        if n is not None:
            body.append(n)
    tree.body = body
    return ast.dump(tree)


def _drop_added_stdlib_imports(old, new):
    """Drop from `new` module-level imports of standard-library modules that `old` lacks, when the
    name they bind is used nowhere else in either version (importing such a module has no effect
    on sqlglot)."""
    std = getattr(sys, "stdlib_module_names", ())
    have = {ast.dump(n) for n in old.body}
    names = set()
    for t in (old, new):
        for x in ast.walk(t):
            if isinstance(x, ast.Name):
                names.add(x.id)
            elif isinstance(x, ast.Attribute):
                names.add(x.attr)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(x.name)
            elif isinstance(x, ast.alias) and x not in getattr(x, "_new", ()):
                pass
    bound_old = set()
    for n in old.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            bound_old |= {(a.asname or a.name).split(".")[0] for a in n.names}
    keep = []
    for n in new.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)) and ast.dump(n) not in have:
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            else:
                mods = [n.module or ""] if not n.level else ["."]
            bound = {(a.asname or a.name).split(".")[0] for a in n.names}
            if all(md.split(".")[0] in std for md in mods) and "*" not in bound \
                    and not (bound & names) and not (bound & bound_old):
                continue
        keep.append(n)
    new.body = keep


class Stmt:
    __slots__ = ("node", "start", "end", "func", "cls", "compound", "parent")


class Index:
    """Statement index of one source file."""

    def __init__(self, tree):
        self.tree = tree
        self.stmts = []
        self._visit(tree.body, None, (), None)
        self.stmts.sort(key=lambda s: (s.start, -s.end))

    def _visit(self, body, func, cls, parent):
        for node in body:
            s = Stmt()
            s.node = node
            start = node.lineno
            for d in getattr(node, "decorator_list", ()):
                start = min(start, d.lineno)
            s.start, s.end = start, node.end_lineno
            s.func = func
            s.cls = cls
            s.compound = isinstance(node, COMPOUND)
            s.parent = parent
            self.stmts.append(s)
            if isinstance(node, FUNCS):
                self._visit(node.body, s, cls, s)
            elif isinstance(node, ast.ClassDef):
                self._visit(node.body, func, cls + (node.name,), s)
            elif s.compound:
                for fld in ("body", "orelse", "finalbody"):
                    sub = getattr(node, fld, None)
                    if sub:
                        self._visit(sub, func, cls, s)
                for h in getattr(node, "handlers", ()):
                    self._visit(h.body, func, cls, s)
                for c in getattr(node, "cases", ()):
                    self._visit(c.body, func, cls, s)

    def innermost(self, line):
        best = None
        for s in self.stmts:
            if s.start > line:
                break
            if s.end >= line and (best is None or (s.end - s.start) <= (best.end - best.start)):
                best = s
        return best

    def enclosing(self, line, kinds):
        """Innermost statement of the given node kinds that strictly spans `line`."""
        best = None
        for s in self.stmts:
            if s.start > line:
                break
            if s.end >= line and isinstance(s.node, kinds) and (best is None or (s.end - s.start) <= (best.end - best.start)):
                best = s
        return best


_src_cache = {}
_idx_cache = {}


def src_lines(path):
    r = _src_cache.get(path)
    if r is None:
        p = SRC / path
        r = _src_cache[path] = p.read_text(encoding="utf-8").split("\n") if p.exists() else None
    return r


def src_index(path):
    r = _idx_cache.get(path)
    if r is None:
        lines = src_lines(path)
        if lines is None:
            return None
        try:
            r = Index(ast.parse("\n".join(lines)))
        except SyntaxError:
            r = None
        _idx_cache[path] = r
    return r


def in_string(idx, line):
    """True when `line` lies inside a multi-line string literal (so a leading # is not a comment)."""
    s = idx.innermost(line)
    if s is None:
        return False
    node = s.node
    if s.compound:
        return False
    for n in ast.walk(node):
        if isinstance(n, (ast.Constant, ast.JoinedStr)) and n.lineno < line <= n.end_lineno:
            if isinstance(n, ast.JoinedStr) or isinstance(n.value, (str, bytes)):
                return True
    return False


def diff_path(a, b):
    """Pairs of corresponding nodes from the roots down to the smallest subtree where two ASTs
    differ, or None when they are equal.  Stops where more than one child differs."""
    if ast.dump(a) == ast.dump(b):
        return None
    path = [(a, b)]
    while True:
        if type(a) is not type(b) or not isinstance(a, ast.AST):
            return path
        diffs = []
        for f in a._fields:
            va, vb = getattr(a, f, None), getattr(b, f, None)
            if isinstance(va, list) or isinstance(vb, list):
                if not (isinstance(va, list) and isinstance(vb, list)) or len(va) != len(vb):
                    return path
                for x, y in zip(va, vb):
                    if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                        if ast.dump(x) != ast.dump(y):
                            diffs.append((x, y))
                    elif x != y:
                        return path
            elif isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                if ast.dump(va) != ast.dump(vb):
                    diffs.append((va, vb))
            elif va != vb:
                return path
        if len(diffs) != 1:
            return path
        a, b = diffs[0]
        path.append((a, b))



_UNKNOWN = object()


def _decode(x):
    if isinstance(x, dict):
        if "tuple" in x:
            items = [_decode(y) for y in x["tuple"]]
            return _UNKNOWN if any(i is _UNKNOWN for i in items) else tuple(items)
        return ("<enum>", x.get("enum"))      # equal only to the same member
    if x == "?":
        return _UNKNOWN
    return x


def _cmp_result(op, flags, a, b):
    """Result of `a op b` from the profile: identity from the recorded flags (when the operands
    are the recorded ones), anything else recomputed from known simple values; None if unknown."""
    if op in ("Is", "IsNot"):
        if flags is not None:
            r = flags[0]
        else:
            a, b = _decode(a), _decode(b)
            if a is _UNKNOWN or b is _UNKNOWN:
                return None
            if not (a is None or b is None or isinstance(a, bool) or isinstance(b, bool)):
                return None
            r = (a is None and b is None) or (isinstance(a, bool) and isinstance(b, bool) and a == b)
        return r if op == "Is" else not r
    if flags is not None and op in ("Eq", "NotEq") and flags[1] is not None:
        return flags[1] if op == "Eq" else not flags[1]
    a, b = _decode(a), _decode(b)
    if a is _UNKNOWN or b is _UNKNOWN:
        return None
    ea = isinstance(a, tuple) and a[:1] == ("<enum>",)
    eb = isinstance(b, tuple) and b[:1] == ("<enum>",)
    if ea or eb:
        if ea and eb and op in ("Eq", "NotEq"):
            r = a == b                       # same member name of the same enum class
            return r if op == "Eq" else not r
        return None                          # str/int-mixin enums can equal plain values
    if any(isinstance(x, tuple) and any(isinstance(y, tuple) and y[:1] == ("<enum>",) for y in x) for x in (a, b)):
        return None
    try:
        return bool({"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
                     "Gt": operator.gt, "GtE": operator.ge}[op](a, b))
    except Exception:
        return None


def body_start(fs):
    """First line of a function's body: its def/decorator lines run at import, not when called."""
    return fs.node.body[0].lineno if fs.node.body else fs.start


def module_of(path):
    p = path[:-3] if path.endswith(".py") else path
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def is_comment_or_blank(text):
    t = text.strip()
    return not t or t.startswith("#")


_all_src = None


def all_sources():
    global _all_src
    if _all_src is None:
        _all_src = sorted(str(p.relative_to(SRC)) for p in (SRC / "sqlglot").rglob("*.py"))
    return _all_src


def defined_names(node):
    names = set()
    if isinstance(node, ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        if isinstance(node.target, ast.Name):
            names.add(node.target.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            n = a.asname or a.name.split(".")[0]
            if n != "*":
                names.add(n)
    return names


# --------------------------------------------------------------- analysis of .py


class Analyzer:
    def __init__(self, m):
        self.m = m

    # -- impact of an import-time (module / class body) statement ------------
    def import_impact(self, path, st, depth=0):
        """Tests affected by a changed statement that runs at import time (module or class body)."""
        m = self.m
        out = set()
        mod = module_of(path)
        idx = src_index(path)
        if st.cls:
            key = mod + ":" + ".".join(st.cls)
            out |= m.class_tests(key)
            if idx:
                for s in idx.stmts:
                    if isinstance(s.node, ast.ClassDef) and s.cls == st.cls[:-1] and s.node.name == st.cls[-1]:
                        out |= m.lines(path, s.start, s.end, imp=True)
                        break
            if not out:
                out |= m.file_union(path)
            return out
        out |= m.file_union(path)
        out |= self.name_uses(path, st, depth)
        return out

    def name_uses(self, path, st, depth, in_class=False):
        """Tests reaching code that uses names defined by statement `st`."""
        m = self.m
        out = set()
        names = defined_names(st.node) if st is not None else set()
        if isinstance(st.node, ast.ClassDef) if st is not None else False:
            names.add(st.node.name)
        if not names:
            return out
        if depth >= 2:
            return out | self.files_tests_all()
        mod = module_of(path)
        base = mod.rsplit(".", 1)[-1]
        if in_class:
            candidates = all_sources()  # class attributes are read through self/cls anywhere
        else:
            candidates = [p for p in all_sources() if p == path or self.imports_module(p, mod, base)]
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        for p in candidates:
            lines = src_lines(p)
            gidx = src_index(p)
            if not lines or gidx is None:
                continue
            for i, text in enumerate(lines, start=1):
                if not pat.search(text) or is_comment_or_blank(text):
                    continue
                s = gidx.innermost(i)
                if s is None or (p == path and st is not None and s.start <= st.start and s.end >= st.end and s.node is st.node):
                    continue
                if isinstance(s.node, (ast.Import, ast.ImportFrom)):
                    continue
                out |= self.stmt_impact(p, gidx, s, i, depth + 1, name_scan=True)
        return out

    def files_tests_all(self):
        return set(self.m.all)

    def imports_module(self, p, mod, base):
        lines = src_lines(p)
        if not lines:
            return False
        txt = "\n".join(lines)
        if mod in txt:
            return True
        parent = mod.rsplit(".", 1)[0]
        return bool(re.search(r"from\s+" + re.escape(parent) + r"\s+import[^\n]*\b" + re.escape(base) + r"\b", txt)) \
            or bool(re.search(r"import\s+" + re.escape(parent) + r"\s*\n", txt))


    # -- a changed plain class attribute: tests that read it -------------------
    def attr_change(self, path, chain, names, skip_lines, same_shape=False):
        """Tests that read class attribute(s) `names` of class `chain` in `path` (or any attribute
        that class-creation hooks derive from them), or None when the change cannot be analysed
        that way (the caller then falls back to a wider selection)."""
        m = self.m
        mod = module_of(path)
        cname = mod + ":" + ".".join(chain)
        if cname not in m.subs:
            return None
        for name in names:
            if not name or name.startswith("__"):
                return None
        K = {cname}                       # classes whose attribute values may change
        N = set(names)                    # attribute names whose values may change
        hook_ids = set()
        skip = {(path, ln) for ln in skip_lines}
        changed = True
        while changed:
            changed = False
            # attributes that class-creation hooks derive from the changed ones
            for h in m.hooks:
                if not (N & set(h["reads"])):
                    continue
                if h["kind"] != "self":
                    return None            # metaclass logic (reads a class's own values at creation)
                if not any(h["cls"] in m.mro.get(k, (k,)) for k in K):
                    continue               # __init_subclass__ of another hierarchy
                if N & set(h.get("opaque", ())):
                    return None            # unmodelled use inside the hook
                if not same_shape:
                    return None            # a hook could fail on a value of another shape
                hook_ids.add((h["file"], h["qual"]))
                derived = {w for w, rs in h["deps"].items() if N & set(rs)}
                if derived - N:
                    N |= derived
                    changed = True
            # import-time code (class bodies, module level) that uses the changed values
            root_names = {k.split(":")[1].split(".")[-1] for k in K}
            for name in list(N):
                for f, cls, targets, base, kind, line in m.body_refs.get(name, ()):
                    if (f, line) in skip:
                        continue
                    d = module_of(f) + ":" + cls
                    if kind == "string":
                        return None
                    if kind == "bare":
                        if d not in K:
                            continue        # another class's own name, or a module global
                    elif base not in root_names:
                        continue            # read from an unrelated class
                    if not cls or not targets or None in targets:
                        return None         # copied into a module constant / something else
                    if d not in K:
                        K.add(d)
                        changed = True
                    if set(targets) - N:
                        N |= set(targets)   # derived in that class body under another name
                        changed = True
        for k in K:
            if k not in m.subs:
                return None
        owners = set()
        for k in K:
            owners |= m.subs[k]
        # reads made by functions while modules were imported or classes created
        for n in N:
            for fid, fowners in m.attr_fn.get(n, {}).items():
                if not owners & set(fowners):
                    continue
                fname, _, qual = fid.partition(":")
                if fname in ("covplugin.py", "brhooks.py"):
                    continue
                if any(fname == hf and (qual == hq or qual.startswith(hq + ".")) for hf, hq in hook_ids):
                    continue
                return None
        out = set()
        for n in N:
            reads = m.attr.get(n, {})
            for o in owners:
                sid = reads.get(o)
                if sid is not None:
                    out |= m.set_tests(sid)
        return out

    def class_stmt_attr(self, path, s, extra_new=None):
        """If statement `s` is a plain assignment in a class body, the attribute names it sets."""
        node = s.node
        if s.func is not None or not s.cls:
            return None
        if isinstance(node, ast.Assign):
            tg = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            tg = [node.target]
        else:
            return None
        names = []
        for t in tg:
            if not isinstance(t, ast.Name):
                return None
            names.append(t.id)
        v = node.value
        if isinstance(v, (ast.Lambda,)) or isinstance(v, ast.Name) and v.id[:1].islower():
            return None   # functions / aliases of functions are not recorded attributes
        return names


    def compare_change(self, path, old_node, new_node):
        """If the only difference between two versions of a statement (or header) lies inside one
        comparison, the tests that evaluated that comparison and could get a different result
        (judged from the comparison profile); None when that does not apply."""
        m = self.m
        if not m.cmp_sites:
            return None
        dp = diff_path(old_node, new_node)
        if not dp:
            return None
        oc = nc = None
        for a, b in dp:
            if isinstance(a, ast.Compare) and isinstance(b, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1:
                oc, nc = a, b
        if oc is None:
            return None
        site = "%s:%d:%d" % (path, oc.lineno, oc.col_offset)
        if site not in m.cmp_sites:
            return None
        o, n = type(oc.ops[0]).__name__, type(nc.ops[0]).__name__
        left_same = ast.dump(oc.left) == ast.dump(nc.left)
        right_same = ast.dump(oc.comparators[0]) == ast.dump(nc.comparators[0])
        lit = {}
        for side, same, node in (("l", left_same, nc.left), ("r", right_same, nc.comparators[0])):
            if not same:
                try:
                    lit[side] = ast.literal_eval(node)
                except Exception:
                    lit[side] = "?"           # not a literal: unknown value

        def unsafe(vs):
            v = json.loads(vs)
            r_old = _cmp_result(o, v, v[2], v[3])
            a = v[2] if left_same else lit["l"]
            b = v[3] if right_same else lit["r"]
            if left_same and right_same:
                r_new = _cmp_result(n, v, a, b)
            else:
                r_new = _cmp_result(n, None, a, b)
            return r_old is None or r_new is None or r_old != r_new

        if any(unsafe(v) for v in m.cmp_import.get(site, ())):
            return None                     # evaluated at import time with a result that could change
        out = set()
        for v, sid in m.cmp.get(site, {}).items():
            if unsafe(v):
                out |= m.set_tests(sid)
        return out

    def inside_header(self, path, idx, old_lines, rem, add, pos):
        """A chunk confined to the header of an if/while/for/with inside a function (its condition,
        iterable or context expressions): the tests that reached that header."""
        if rem:
            first, last = rem[0][0], rem[-1][0]
        else:
            first, last = pos + 1, pos
        s = None
        for c in idx.stmts:
            if c.start > first:
                break
            if c.compound and c.func is not None and isinstance(c.node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)) \
                    and c.start <= first and c.node.body[0].lineno > max(last, first):
                if s is None or c.start >= s.start:
                    s = c
        if s is None:
            return None
        hend = s.node.body[0].lineno - 1
        if rem and last > hend or not rem and pos >= hend:
            return None
        # the patched header must still parse as the same kind of statement around the old body
        if rem:
            new = old_lines[s.start - 1:first - 1] + add + old_lines[last:s.end]
        else:
            new = old_lines[s.start - 1:pos] + add + old_lines[pos:s.end]
        import textwrap
        try:
            tree = ast.parse(textwrap.dedent("\n".join(new)))
        except SyntaxError:
            return None
        if len(tree.body) != 1 or type(tree.body[0]) is not type(s.node):
            return None
        nb, ob = tree.body[0], s.node
        if ast.dump(ast.Module(nb.body, [])) != ast.dump(ast.Module(ob.body, [])) or \
                ast.dump(ast.Module(getattr(nb, "orelse", []), [])) != ast.dump(ast.Module(getattr(ob, "orelse", []), [])):
            return None
        hdr_old = getattr(ob, "test", None) or getattr(ob, "iter", None)
        hdr_new = getattr(nb, "test", None) or getattr(nb, "iter", None)
        if hdr_old is not None and hdr_new is not None:
            r = self.compare_change(path, hdr_old, hdr_new)
            if r is not None:
                return r
        if self.m.import_ran(path, s.start, hend):
            return ALL
        return self.m.lines(path, s.start, hend)

    def inside_statement(self, path, idx, old_lines, rem, add, pos):
        """A chunk that only rewrites the inside of one simple statement: the tests that ran it
        (or, for a class attribute, that read it).  None when the chunk is anything else."""
        hdr = self.inside_header(path, idx, old_lines, rem, add, pos)
        if hdr is not None:
            return hdr
        if rem:
            first, last = rem[0][0], rem[-1][0]
            s = idx.innermost(first)
            if s is None or s.compound or not (s.start <= first and last <= s.end):
                return None
            if any(idx.innermost(n) is not s for n, _ in rem if not is_comment_or_blank(old_lines[n - 1])):
                return None
            new = old_lines[s.start - 1:first - 1] + add + old_lines[last:s.end]
        else:
            s = idx.innermost(pos)
            if s is None or s.compound or not (s.start <= pos < s.end):
                return None
            new = old_lines[s.start - 1:pos] + add + old_lines[pos:s.end]
        if isinstance(s.node, FUNCS + (ast.ClassDef,)):
            return None
        import textwrap
        try:
            tree = ast.parse(textwrap.dedent("\n".join(new)))
        except SyntaxError:
            return None
        if len(tree.body) != 1 or type(tree.body[0]) is not type(s.node):
            return None
        if ast.dump(tree.body[0]) == ast.dump(s.node):
            return set()          # only comments or layout changed inside the statement
        r = self.compare_change(path, s.node, tree.body[0])
        if r is not None:
            return r
        m = self.m
        line = rem[0][0] if rem else pos
        names = self.class_stmt_attr(path, s)
        if names:
            ns = Stmt()
            ns.node, ns.func, ns.cls, ns.start, ns.end, ns.compound, ns.parent = tree.body[0], None, s.cls, 0, 0, False, None
            new_names = self.class_stmt_attr(path, ns)
            if new_names:
                lam = any(isinstance(n, ast.Lambda) and n.lineno <= line <= n.end_lineno for n in ast.walk(s.node))
                if not lam:
                    ov, nv = s.node.value, tree.body[0].value
                    same = type(ov) is type(nv) and (not isinstance(ov, ast.Constant) or type(ov.value) is type(nv.value))
                    r = self.attr_change(path, s.cls, sorted(set(names) | set(new_names)), set(range(s.start, s.end + 1)),
                                         same_shape=same)
                    if r is not None:
                        return r
        return self.stmt_impact(path, idx, s, line)

    # -- impact of statement containing old line ------------------------------
    def stmt_impact(self, path, idx, s, line, depth=0, name_scan=False):
        m = self.m
        node = s.node
        # function definition header (signature, decorators, def line) or a line of a def not in a child
        if isinstance(node, FUNCS):
            if m.import_ran(path, body_start(s), s.end):
                return ALL
            return m.lines(path, body_start(s), s.end)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return set()          # a docstring / bare string: no runtime effect (doctests are not in the suite)
        if isinstance(node, ast.ClassDef):
            own = Stmt()
            own.node, own.start, own.end = node, s.start, s.end
            own.func, own.parent, own.compound = s.func, s.parent, True
            own.cls = s.cls + (node.name,)  # a change to the class header is a change to the class itself
            return self.import_impact(path, own, depth)
        if s.func is not None:
            if m.import_ran(path, s.start, s.end):
                return ALL     # runs at import time: what it computes outlives any one test
            return m.lines(path, s.start, s.end)
        # import-time statement (module or class body)
        lam = None
        for n in ast.walk(node):
            if isinstance(n, ast.Lambda) and n.lineno <= line <= n.end_lineno:
                lam = n
                break
        if lam is not None:
            b = lam.body
            if b.lineno > lam.lineno and m.import_ran(path, b.lineno, b.end_lineno):
                return ALL
            return m.lines(path, lam.lineno, lam.end_lineno) | m.lines(path, line, line)
        if s.compound:
            return m.lines(path, s.start, s.end, imp=True) | self.import_impact(path, s, depth)
        names = self.class_stmt_attr(path, s)
        if names and not name_scan:
            r = self.attr_change(path, s.cls, names, set(range(s.start, s.end + 1)))
            if r is not None:
                return r | (m.lines(path, lam.lineno, lam.end_lineno) if lam is not None else set())
        return self.import_impact(path, s, depth)

    # -- one python source file under sqlglot/ --------------------------------
    def analyze(self, fp):
        m = self.m
        path = fp.old or fp.new
        if fp.is_deleted:
            return import_failure(m, path) if path.endswith(".py") else ALL     # importing it fails
        if fp.is_new or path not in {p for p in all_sources()}:
            return self.new_module(fp)
        old_lines = src_lines(path)
        if old_lines is None:
            return self.new_module(fp)
        idx = src_index(path)
        new_lines = apply_hunks(old_lines, fp)
        if new_lines is None or idx is None:
            return ALL
        try:
            ast.parse("\n".join(new_lines))
        except SyntaxError:
            return import_failure(m, path)
        if fp.new and fp.old and fp.new != fp.old:
            # renamed: nothing imports the new name, and importing the old one fails
            return import_failure(m, fp.old) if fp.old.endswith(".py") and fp.new.endswith(".py") else ALL
        if types_only_change("\n".join(old_lines), "\n".join(new_lines)):
            return set()
        out = set()
        for rem, add, pos in chunks(fp):
            real_rem = [(n, t) for n, t in rem if not is_comment_or_blank(t) or in_string(idx, n)]
            real_add = [t for t in add if not is_comment_or_blank(t)]
            if not real_add and add and (in_string(idx, pos) or in_string(idx, pos + 1)):
                real_add = add
            if not real_rem and not real_add:
                continue
            r = self.inside_statement(path, idx, old_lines, rem, add, pos)
            if r is not None:
                out |= r
                continue
            if real_rem:
                seen = set()
                for n, t in real_rem:
                    s = idx.innermost(n)
                    if s is None:
                        out |= m.file_union(path)
                        continue
                    key = (s.start, s.end, n if not s.compound else 0)
                    if key in seen:
                        continue
                    seen.add(key)
                    out |= self.stmt_impact(path, idx, s, n)
                if real_add:
                    out |= self.added_impact(path, idx, pos, real_add, near=True)
            else:
                out |= self.added_impact(path, idx, pos, real_add, near=False)
            if out is ALL:
                return ALL
        return out

    def added_impact(self, path, idx, pos, added, near):
        """Added lines after old line `pos`: tests that run the surrounding block."""
        m = self.m
        m_indent = min(len(t) - len(t.lstrip()) for t in added)
        out = set()
        fn = None
        for s in idx.stmts:
            if s.start > pos + 1:
                break
            if isinstance(s.node, FUNCS) and s.start <= pos < s.end or isinstance(s.node, FUNCS) and s.start <= pos + 1 <= s.end and s.start <= pos:
                fn = s if fn is None or (s.end - s.start) <= (fn.end - fn.start) else fn
            elif isinstance(s.node, FUNCS) and pos == s.end:
                def_indent = s.node.col_offset
                if m_indent > def_indent:
                    fn = s if fn is None or (s.end - s.start) <= (fn.end - fn.start) else fn
        if fn is not None:
            if m.import_ran(path, body_start(fn), fn.end):
                return ALL
            return m.lines(path, body_start(fn), fn.end)
        # class body or module level
        cls = None
        for s in idx.stmts:
            if s.start > pos + 1:
                break
            if isinstance(s.node, ast.ClassDef) and s.start <= pos < s.end:
                cls = s if cls is None or (s.end - s.start) <= (cls.end - cls.start) else cls
            elif isinstance(s.node, ast.ClassDef) and pos == s.end and m_indent > s.node.col_offset:
                cls = s if cls is None or (s.end - s.start) <= (cls.end - cls.start) else cls
        mod = module_of(path)
        if cls is not None:
            chain = []
            for s in idx.stmts:
                if isinstance(s.node, ast.ClassDef) and s.start <= cls.start and s.end >= cls.end:
                    chain.append(s)
            chain.sort(key=lambda s: s.start)
            names = tuple(s.node.name for s in chain)
            key = mod + ":" + ".".join(names)
            out |= m.class_tests(key)
            out |= m.lines(path, cls.start, cls.end, imp=True)
            if not out:
                out |= m.file_union(path)
            # new names defined in the class may be read from base classes via self.X
            return out
        # module level addition
        if all(re.match(r"\s*(def |class |@|async def |[A-Za-z_]\w*\s*(:[^=]+)?=|\)|\]|\}|\s)", t) for t in added) \
                and self.defs_only(added):
            return self.name_uses_text(path, added, in_class=False)
        out |= m.file_union(path)
        out |= self.name_uses_text(path, added, in_class=False)
        return out

    @staticmethod
    def defs_only(added):
        try:
            tree = ast.parse("\n".join(added))
        except SyntaxError:
            return False
        return all(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body)

    @staticmethod
    def has_def(added):
        return any(re.match(r"\s*(def|class|[A-Za-z_]\w*\s*[:=])", t) for t in added)

    def name_uses_text(self, path, added, in_class):
        names = set()
        for t in added:
            m_ = re.match(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)", t) or re.match(r"\s*([A-Za-z_]\w*)\s*(?::[^=]*)?=", t)
            if m_:
                names.add(m_.group(1))
        if not names:
            return set()
        # a new name can shadow or be read by existing code: look for existing uses
        fake = Stmt()
        fake.node = ast.parse("\n".join(f"{n} = 0" for n in names)).body[0]
        fake.start = fake.end = 10**9
        fake.cls = ()
        fake.func = None
        out = set()
        mod = module_of(path)
        base = mod.rsplit(".", 1)[-1]
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        cands = all_sources() if in_class else [p for p in all_sources() if p == path or self.imports_module(p, mod, base)]
        for p in cands:
            lines = src_lines(p)
            gidx = src_index(p)
            if not lines or gidx is None:
                continue
            for i, text in enumerate(lines, start=1):
                if pat.search(text) and not is_comment_or_blank(text):
                    s = gidx.innermost(i)
                    if s is None or isinstance(s.node, (ast.Import, ast.ImportFrom)):
                        continue
                    out |= self.stmt_impact(p, gidx, s, i, 1, name_scan=True)
        return out

    def new_module(self, fp):
        m = self.m
        out = set()
        for f in ("tests/test_dialect_imports.py", "tests/test_typing.py", "tests/test_dialect_entry_points.py",
                  "tests/test_sqlglotc.py", "tests/test_build.py", "tests/test_docs.py"):
            out |= set(m.by_file.get(f, ()))
        return out


# ---------------------------------------------------------------------- selection


_importers_cache = {}


def test_importers(m, path):
    """Tests of every test file that imports test module `path`, directly or through another."""
    r = _importers_cache.get(path)
    if r is not None:
        return r
    files = {path}
    frontier = [path]
    texts = {}
    for f in m.by_file:
        lines = src_lines(f)
        if lines is not None:
            texts[f] = "\n".join(lines)
    while frontier:
        p = frontier.pop()
        mod = p[:-3].replace("/", ".")
        pkg, _, base = mod.rpartition(".")
        pat = re.compile(r"(^|\n)\s*(from\s+%s\s+import|import\s+%s\b|from\s+%s\s+import[^\n]*\b%s\b|from\s+\.\s+import[^\n]*\b%s\b|from\s+\.%s\s+import)"
                         % (re.escape(mod), re.escape(mod), re.escape(pkg), re.escape(base), re.escape(base), re.escape(base)))
        for f, t in texts.items():
            if f not in files and pat.search(t):
                files.add(f)
                frontier.append(f)
    out = set()
    for f in files:
        out |= set(m.by_file.get(f, ()))
    _importers_cache[path] = out
    return out


# ------------------------------------------------------------------- import failures
# A module that cannot be imported (a syntax error, say) fails exactly the tests that try to
# import it: test files that import it (or a module that imports it) when collected, tests that
# run an import statement reaching it, tests that ran code of a module that imports it, and tests
# that made a dialect whose module imports it (dialects are imported by name, see dialect_users).

_imp = {"mods": None, "top": {}, "clo": {}, "lazy": {}, "dyn": None, "users": None, "trees": {}}


def py_modules():
    if _imp["mods"] is None:
        mods = {}
        for p in SRC.rglob("*.py"):
            rel = p.relative_to(SRC).as_posix()
            name = rel[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[:-9]
            mods[name] = rel
        _imp["mods"] = mods
    return _imp["mods"]


def lazy_targets(pkg, attr):
    """Modules a package's module-level __getattr__ may import to give `attr`."""
    mods = py_modules()
    rel = mods.get(pkg)
    if rel is None:
        return []
    if pkg not in _imp["lazy"]:
        text = (SRC / rel).read_text()
        tree = ast.parse(text)
        has = any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body)
        names = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            names.add(x.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
        _imp["lazy"][pkg] = (has, text, names)
    has, text, names = _imp["lazy"][pkg]
    if not has or attr in names:
        return []
    found = [mm.group(1) for mm in re.finditer(r'"%s"\s*:\s*\(\s*"([\w.]+)"' % re.escape(attr), text)]
    found += [pkg + "." + mm.group(1) for mm in re.finditer(r'"%s"\s*:\s*"(\w+)"' % re.escape(attr), text)]
    if pkg + "." + attr.lower() in mods:
        found.append(pkg + "." + attr.lower())
    found = [f for f in found if f in mods]
    if not found:   # unknown: any submodule
        found = [x for x in mods if x.startswith(pkg + ".") and x.count(".") == pkg.count(".") + 1]
    return found


def _import_targets(cur, is_pkg, node):
    """Modules (and their parent packages) an import statement imports."""
    mods = py_modules()
    out = []
    if isinstance(node, ast.Import):
        out = [a.name for a in node.names]
    else:
        base = node.module or ""
        if node.level:
            parts = cur.split(".")
            if not is_pkg:
                parts = parts[:-1]
            if node.level > 1:
                parts = parts[:len(parts) - (node.level - 1)]
            base = ".".join(parts + ([base] if base else []))
        out.append(base)
        for a in node.names:
            if base + "." + a.name in mods:
                out.append(base + "." + a.name)
            else:
                out.extend(lazy_targets(base, a.name))
    res = set()
    for n in out:
        parts = n.split(".")
        for i in range(1, len(parts) + 1):
            q = ".".join(parts[:i])
            if q in mods:
                res.add(q)
    return res


def module_imports(name, all_levels=False):
    """(line, modules, runs at import time) for the import statements of a module: those that run
    when it is imported (module level, class bodies, try/if, not under TYPE_CHECKING) or, with
    all_levels, every one."""
    key = (name, all_levels)
    r = _imp["top"].get(key)
    if r is not None:
        return r
    rel = py_modules()[name]
    tree = _imp["trees"].get(rel)
    if tree is None:
        try:
            tree = ast.parse((SRC / rel).read_text())
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
        _imp["trees"][rel] = tree
    is_pkg = rel.endswith("__init__.py")
    found = []

    def walk(body, top):
        for n in body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                found.append((n.lineno, _import_targets(name, is_pkg, n), top))
            elif isinstance(n, ast.If) and _is_type_checking(n.test):
                walk(n.orelse, top)
                if all_levels:
                    walk(n.body, False)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(n.body, False)
            else:
                for f in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(n, f, None)
                    if isinstance(sub, list):
                        walk(sub, top)
    walk(tree.body, True)
    _imp["top"][key] = found
    return found


def _is_type_checking(test):
    return any(isinstance(x, ast.Name) and x.id == "TYPE_CHECKING" or isinstance(x, ast.Attribute) and x.attr == "TYPE_CHECKING"
               for x in ast.walk(test))


def import_closure(name):
    """Every module imported when `name` is imported."""
    r = _imp["clo"].get(name)
    if r is not None:
        return r
    mods = py_modules()
    seen = set()
    parts = name.split(".")
    stack = [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
    while stack:
        q = stack.pop()
        if q in seen or q not in mods:
            continue
        seen.add(q)
        for _, ms, top in module_imports(q):
            if top:
                for x in ms:
                    p = x.split(".")
                    stack.extend(".".join(p[:i]) for i in range(1, len(p) + 1))
    _imp["clo"][name] = seen
    return seen


def dialect_users(m):
    """Dialect module -> tests that made or looked up one of its dialect classes."""
    if _imp["users"] is None:
        users = {}
        p = APP / "data" / "dialect_users.txt"
        lines = [x for x in (p.read_text().splitlines() if p.exists() else ()) if x.strip() and not x.startswith("#")]
        cur = None
        ds = None
        for line in lines:
            if line.startswith("@ "):
                cur, ds = line[2:].strip(), None
            elif ds is None:
                ds = line.split()
            else:
                cls = None
                for item in line.split():
                    c, _, name = item.rpartition("::")
                    cls = c or cls
                    n = m.num.get("%s::%s::%s" % (cur, cls, name) if cls else "%s::%s" % (cur, name))
                    if n is not None:
                        for d in ds:
                            users.setdefault("sqlglot.dialects." + d, set()).add(n)
                ds = None
        _imp["users"] = users
    return _imp["users"]


def _dynamic_keys():
    """Test ids (or class prefixes ending in '::') whose code imports modules by a computed name
    or runs Python in a subprocess."""
    keys = []
    pat = re.compile(r"importlib|pkgutil|__import__|import_module|runpy|subprocess")
    for p in sorted((SRC / "tests").rglob("test_*.py")):
        rel = p.relative_to(SRC).as_posix()
        lines = src_lines(rel)
        if lines is None:
            continue
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            keys.append(rel + "::")
            continue

        def walk(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    walk(n.body, prefix + [n.name])
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and pat.search("\n".join(lines[n.lineno - 1:n.end_lineno])):
                    if n.name.startswith("test"):
                        keys.append("::".join([rel] + prefix + [n.name]))
                    else:   # a helper or setUp: the whole class
                        keys.append("::".join([rel] + prefix) + "::")
        walk(tree.body, [])
    return keys


def _load_import_cache():
    if "cache" not in _imp:
        c = None
        p = APP / "data" / "imports.marshal"
        if p.exists():
            import marshal
            with open(p, "rb") as f:
                c = marshal.load(f)
            _imp["mods"] = c["mods"]
            for name, lst in c["top"].items():
                _imp["top"][(name, False)] = [(ln, set(ms), top) for ln, ms, top in lst if top]
                _imp["top"][(name, True)] = [(ln, set(ms), top) for ln, ms, top in lst]
        _imp["cache"] = c
    return _imp["cache"]


def build_import_cache(out):
    import marshal
    mods = py_modules()
    top = {name: [(ln, sorted(ms), t) for ln, ms, t in module_imports(name, all_levels=True)] for name in mods}
    with open(out, "wb") as f:
        marshal.dump({"mods": mods, "top": top, "dyn": _dynamic_keys()}, f)


def dynamic_importers(m):
    if _imp["dyn"] is None:
        c = _load_import_cache()
        keys = c["dyn"] if c else _dynamic_keys()
        acc = set()
        for k in keys:
            if k.endswith("::"):
                acc.update(m.num[i] for i in m.num if i.startswith(k))
            elif k in m.num:
                acc.add(m.num[k])
        _imp["dyn"] = acc
    return _imp["dyn"]


def _test_importers_by_graph(m, name):
    """Tests of test module `name` and of every test module importing it, directly or not."""
    r = _imp.setdefault("timp", {}).get(name)
    if r is not None:
        return r
    mods = py_modules()
    rev = _imp.get("trev")
    if rev is None:
        rev = {}
        for x, rel in mods.items():
            if rel.startswith("tests/"):
                for _, ms, _ in module_imports(x, all_levels=True):
                    for y in ms:
                        rev.setdefault(y, set()).add(x)
        _imp["trev"] = rev
    seen = {name}
    stack = [name]
    while stack:
        for y in rev.get(stack.pop(), ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    out = set()
    for x in seen:
        out.update(m.by_file.get(mods[x], ()))
    _imp["timp"][name] = out
    return out


def import_failure(m, path):
    """Tests that fail when the sqlglot module at `path` raises on import."""
    _load_import_cache()
    mods = py_modules()
    name = path[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[:-9]
    if name not in mods:
        return ALL
    dep = {x for x in mods if name in import_closure(x)}     # modules that cannot be imported
    out = set()
    users = dialect_users(m)
    for x in dep:
        rel = mods[x]
        if rel.startswith("sqlglot/"):
            out |= m.file_union(rel)
        if x.startswith("sqlglot.dialects."):
            out |= users.get(x, set())
    for x, rel in mods.items():
        for ln, ms, top in module_imports(x, all_levels=True):
            if not (ms & dep):
                continue
            if rel.startswith("tests/"):
                out |= _test_importers_by_graph(m, x)
            elif not top:
                out |= m.lines(rel, ln, ln)
    out |= dynamic_importers(m)
    _imp["hit"] = True
    log("import failure", path, len(dep), "modules", len(out), "tests")
    return out


def _qual(tree, fn):
    """(Class, ..., name) path of function node `fn` inside `tree`."""
    def walk(body, prefix):
        for x in body:
            if x is fn:
                return prefix + (x.name,)
            if isinstance(x, ast.ClassDef):
                r = walk(x.body, prefix + (x.name,))
                if r:
                    return r
        return None
    return walk(tree.body, ())


def test_file_impact(m, fp):
    path = fp.old or fp.new
    in_file = test_importers(m, path)      # the file's tests and those of files importing it
    if fp.is_new:
        return set()
    if fp.is_deleted:
        return in_file
    old_lines = src_lines(path)
    if old_lines is None:
        return in_file
    new_lines = apply_hunks(old_lines, fp)
    if new_lines is None:
        return in_file
    try:
        a = ast.parse("\n".join(old_lines))
        b = ast.parse("\n".join(new_lines))
    except SyntaxError:
        return in_file
    if ast.dump(strip_docstrings(a)) == ast.dump(strip_docstrings(b)):
        return set()

    def funcs(tree):
        res = {}
        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []
            def visit_ClassDef(self, n):
                self.stack.append(n.name); self.generic_visit(n); self.stack.pop()
            def visit_FunctionDef(self, n):
                res[tuple(self.stack) + (n.name,)] = n
                # nested functions belong to the outer function
            visit_AsyncFunctionDef = visit_FunctionDef
        V().visit(tree)
        return res

    def skeleton(tree):
        t = ast.parse(ast.unparse(tree))
        for n in ast.walk(t):
            if isinstance(n, FUNCS):
                n.body = [ast.Pass()]
        return ast.dump(strip_docstrings(t))

    fa, fb = funcs(a), funcs(b)
    added = {k for k in fb if k not in fa}
    if added and all(k[-1].startswith("test") for k in added):
        # new test methods only: drop them before comparing (they are not in tests.json and
        # cannot change existing tests unless they reuse a name, which `added` excludes)
        b2 = ast.parse(ast.unparse(b))
        for n in ast.walk(b2):
            if isinstance(n, (ast.Module, ast.ClassDef)):
                stack = []
                n.body = [x for x in n.body if not (isinstance(x, FUNCS) and x.name.startswith("test")
                                                     and _qual(b2, x) in added)] or [ast.Pass()]
        b = b2
        fb = funcs(b)
    if skeleton(a) != skeleton(b):
        return in_file
    changed = set()
    for k in set(fa) | set(fb):
        if k not in fa or k not in fb or ast.dump(strip_docstrings(ast.Module([fa[k]], []))) != ast.dump(strip_docstrings(ast.Module([fb[k]], []))):
            changed.add(k)
    out = set()
    for k in changed:
        if not k[-1].startswith("test"):
            return in_file
        pre = path + "::" + "::".join(k)
        hit = [t["number"] for t in m.tests if t["id"] == pre]
        if not hit:
            return in_file
        out |= set(hit)
        # the same method inherited by test classes in other files
        out |= {n for n in in_file if m.tests[n]["id"].endswith("::" + k[-1])}
    return out


def fixture_impact(m, fp):
    path = fp.new or fp.old
    out = m.conv(m.opens.get(path, ()))
    if out:
        if path.endswith((".gz", ".csv", ".parquet")):
            # table data is loaded once per class: every test in the opening files may use it
            files = {m.tests[n]["file"] for n in out}
            for f in files:
                out |= set(m.by_file.get(f, ()))
        return out
    if path in m.refs and m.refs[path]:
        res = set()
        for f in m.refs[path]:
            res |= set(m.by_file.get(f, ()))
        return res
    return ALL if path in m.refs or fp.is_new is False else set()


def select_for_commit(m, an, patch_text):
    files = parse_patch(patch_text)
    if not files:
        return ALL
    out = set()
    for fp in files:
        path = fp.new or fp.old or ""
        old = fp.old or path
        try:
            r = select_for_file(m, an, fp, path, old)
        except Exception as e:  # never guess low on an unexpected shape
            log("analysis error", path, repr(e))
            r = ALL
        if r is ALL:
            return ALL
        out |= r
    return out


def select_for_file(m, an, fp, path, old):
    ext = Path(path).suffix.lower()
    if path.startswith("sqlglot/") and path.endswith(".py"):
        return an.analyze(fp)
    if path.startswith("tests/fixtures/"):
        return fixture_impact(m, fp)
    if path.startswith("tests/") and path.endswith(".py"):
        base = Path(path).name
        if base.startswith("test_"):
            return test_file_impact(m, fp)
        if base in ("__init__.py", "conftest.py") or fp.is_new or fp.is_deleted or fp.old != fp.new:
            return ALL
        return test_importers(m, path)      # shared test code such as tests/helpers.py
    if path.startswith("sqlglotc/"):
        return set(m.by_file.get("tests/test_sqlglotc.py", ())) | (ALL if not path.endswith(tuple(DOC_EXT)) else set())
    if path.startswith("sqlglot/"):
        return set() if ext in DOC_EXT else ALL
    if ext in DOC_EXT or path.startswith((".github/", "posts/", "pdoc/", "benchmarks/")) or Path(path).name in {
            "LICENSE", "CHANGELOG.md", "README.md", ".gitignore"}:
        return set()
    return ALL


# ------------------------------------------------------------------------ workers


def split(tests_by_num, selected):
    """Choose worker count and layout minimising busiest-worker + 2 s per worker."""
    if not selected:
        return []
    items = sorted(((round(tests_by_num[n]["seconds"] * 1000), n) for n in selected), reverse=True)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for d, n in items:
            i = loads.index(min(loads))
            loads[i] += d
            ws[i].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
_tp = {}
TPCDS_BUDGET = 75.0  # wall seconds of checks per run; past it the test is simply kept


def tpcds_unchanged(text):
    """True when rerunning the TPC-DS pipeline on the patched sqlglot (tpcheck.py) shows no
    difference from the pinned one, so test_execute_tpcds cannot change outcome."""
    import pickle
    import tpgate
    import time
    data = APP / "data" / "tpc.pkl"
    if not data.exists() or _tp.get("spent", 0.0) > TPCDS_BUDGET:
        return False
    t0 = time.monotonic()
    try:
        if "rt" not in _tp:
            with open(data, "rb") as f:
                d = pickle.load(f)
            _tp["rt"], _tp["q"], _tp["n"] = d["rt"], d["qlines"], len(d["queries"])
            _tp["mods"] = set(d["mods"]) if "mods" in d else None
        files = tpgate.relevant(parse_patch(text), apply_hunks, APP / "data" / "tpc-ds.sql")
        if not files:
            return True
        if tpgate.touches(files, chunks, _tp["rt"]):
            return False
        mask = tpgate.queries_reached(files, SRC, chunks, _tp["q"])
        only = None if mask is None else [i for i in range(_tp["n"]) if mask >> i & 1]
        if only == []:
            return tpgate.applies(files, SRC, apply_hunks)
        return tpgate.unchanged(files, SRC, apply_hunks, data, APP / "tpcheck.py", only, mods=_tp["mods"])
    except Exception:  # noqa: BLE001
        return False
    finally:
        _tp["spent"] = _tp.get("spent", 0.0) + time.monotonic() - t0


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_num = {t["number"]: t for t in tests}
    m = Map(tests)
    an = Analyzer(m)
    tpcds = next((t["number"] for t in tests if t["id"] == TPCDS_ID), None)
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        _imp["hit"] = False
        sel = select_for_commit(m, an, text)
        if sel is ALL:
            sel = set(by_num)
        elif tpcds in sel and not _imp["hit"] and tpcds_unchanged(text):
            sel = sel - {tpcds}
        workers = split(by_num, sel)
        total += len(sel)
        log(patch.stem, len(sel))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    log("total selected", total)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--imports"]:
        build_import_cache(sys.argv[2])
    else:
        main()
