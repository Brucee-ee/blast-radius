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
import re
import sys
from pathlib import Path

import os
DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
SRC = APP / "src"
WORKER_COST = 2.0
MAX_WORKERS = 8
ALL = "ALL"
IMPORT_CTX = -1

DOC_EXT = {".md", ".rst", ".txt", ".png", ".svg", ".jpg", ".gif"}


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, tests):
        m = json.loads((APP / "data" / "map.json").read_text())
        num = {t["id"]: t["number"] for t in tests}
        self.num = num
        self.tests = tests
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.id_to_num = [num.get(i) for i in m["ids"]]
        self.sets = m["sets"]
        self.files = {f: {int(k): v for k, v in d.items()} for f, d in m["files"].items()}
        self.classes = m["classes"]
        self.opens = m["opens"]
        self.refs = m["refs"]
        self._set_cache = {}
        self.all = set(t["number"] for t in tests)
        self.attr = m.get("attr", {})
        self.attr_fn = {k: set(v) for k, v in m.get("attr_fn", {}).items()}
        self.body_refs = m.get("body_refs", {})
        self.mro = m.get("mro", {})
        self.creation_names = set(m.get("creation_names", ()))
        self.subs = {}          # class key -> owner keys that have it in their MRO
        for o, chain in self.mro.items():
            for c in chain:
                self.subs.setdefault(c, set()).add(o)

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

    def lines(self, path, start, end):
        d = self.files.get(path)
        out = set()
        if not d:
            return out
        for ln in range(start, end + 1):
            sid = d.get(ln)
            if sid is not None:
                out |= self.set_tests(sid)
        return out

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
        d = self.files.get(path) or {}
        out = set()
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
                        out |= m.lines(path, s.start, s.end)
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
    def attr_change(self, path, chain, names, skip_lines):
        """Tests that read class attribute(s) `names` of class `chain` in `path`, or None when the
        change cannot be analysed that way (caller then falls back to a wider selection)."""
        m = self.m
        mod = module_of(path)
        cname = mod + ":" + ".".join(chain)
        if cname not in m.subs:
            return None
        out = set()
        for name in names:
            if not name or name.startswith("__") or name in m.creation_names:
                return None
            roots = {cname}
            root_names = {chain[-1]}
            refs = [r for r in m.body_refs.get(name, ())
                    if not (r[0] == path and r[5] in skip_lines)]
            changed = True
            while changed:
                changed = False
                for f, cls, targets, base, kind, line in refs:
                    d = module_of(f) + ":" + cls
                    if kind == "string":
                        return None
                    if kind == "bare":
                        if d in roots and targets != [name]:
                            return None
                        continue
                    if base in root_names:
                        if not cls or targets != [name]:
                            return None      # copied into a module constant or another name
                        if d not in roots:
                            roots.add(d)
                            root_names.add(cls.split(".")[-1])
                            changed = True
            for r in roots:
                if r not in m.subs:
                    return None
            owners = set()
            for r in roots:
                owners |= m.subs[r]
            if owners & m.attr_fn.get(name, set()):
                return None
            reads = m.attr.get(name, {})
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


    def inside_statement(self, path, idx, old_lines, rem, add, pos):
        """A chunk that only rewrites the inside of one simple statement: the tests that ran it
        (or, for a class attribute, that read it).  None when the chunk is anything else."""
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
                    r = self.attr_change(path, s.cls, sorted(set(names) | set(new_names)), set(range(s.start, s.end + 1)))
                    if r is not None:
                        return r
        return self.stmt_impact(path, idx, s, line)

    # -- impact of statement containing old line ------------------------------
    def stmt_impact(self, path, idx, s, line, depth=0, name_scan=False):
        m = self.m
        node = s.node
        # function definition header (signature, decorators, def line) or a line of a def not in a child
        if isinstance(node, FUNCS):
            return m.lines(path, s.start, s.end)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return set()          # a docstring / bare string: no runtime effect (doctests are not in the suite)
        if isinstance(node, ast.ClassDef):
            own = Stmt()
            own.node, own.start, own.end = node, s.start, s.end
            own.func, own.parent, own.compound = s.func, s.parent, True
            own.cls = s.cls + (node.name,)  # a change to the class header is a change to the class itself
            return self.import_impact(path, own, depth)
        if s.func is not None:
            res = m.lines(path, s.start, s.end)
            if m.has_import_ctx(path, s.start, s.end):
                # also ran outside any test (collection, a thread, a helper process): widen to the function
                res |= m.lines(path, s.func.start, s.func.end)
            return res
        # import-time statement (module or class body)
        lam = None
        for n in ast.walk(node):
            if isinstance(n, ast.Lambda) and n.lineno <= line <= n.end_lineno:
                lam = n
                break
        if lam is not None and not (s.cls and s.func is None):
            return m.lines(path, lam.lineno, lam.end_lineno) | (m.lines(path, line, line))
        if lam is not None:
            return m.lines(path, lam.lineno, lam.end_lineno) | m.lines(path, line, line)
        if s.compound:
            return m.lines(path, s.start, s.end) | self.import_impact(path, s, depth)
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
            return ALL
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
            return ALL
        if fp.new and fp.old and fp.new != fp.old:
            return ALL  # rename
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
            return m.lines(path, fn.start, fn.end)
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
            out |= m.lines(path, cls.start, cls.end)
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


def test_file_impact(m, fp):
    path = fp.old or fp.new
    in_file = set(m.by_file.get(path, ()))
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

    if skeleton(a) != skeleton(b):
        return in_file
    fa, fb = funcs(a), funcs(b)
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
        out |= set(hit)
        if not hit:
            return in_file
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
        return ALL
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


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_num = {t["number"]: t for t in tests}
    m = Map(tests)
    an = Analyzer(m)
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        sel = select_for_commit(m, an, text)
        if sel is ALL:
            sel = set(by_num)
        workers = split(by_num, sel)
        total += len(sel)
        log(patch.stem, len(sel))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    log("total selected", total)


if __name__ == "__main__":
    main()
