"""Comparison profile (build time only, enabled with BR_PROFILE=1 from sitecustomize).

Every single-operator comparison inside a function or lambda body of a sqlglot module is
compiled as _brcmp_(site, op, left, right), which returns exactly what the comparison returns
(operands evaluated once, in order) and records, for the running test:
  is / is not / == / != : whether `left is right` and `left == right` agreed
  < / <= / > / >=       : whether `left == right` held
Site ids are "sqlglot/<file>.py:<line>:<col>" of the Compare node in the unmodified source."""
import ast
import builtins
import enum
import json
import operator
import os
import sys
from importlib.machinery import SourceFileLoader

import brhooks

PROF = {}          # (site, ctx) -> set(values)
SITES = set()      # every instrumented site
_OPS = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
        "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
_IDENT = {"Is", "IsNot", "Eq", "NotEq"}


_SIMPLE = (type(None), bool, int, float, str, bytes, enum.Enum)


def _simple(x, depth=0):
    if isinstance(x, _SIMPLE):
        return True
    if isinstance(x, tuple) and depth < 2 and len(x) <= 8:
        return all(_simple(y, depth + 1) for y in x)
    return False


def _singleton(x):
    # comparing anything with None, a bool or an enum member is cheap
    return x is None or isinstance(x, (bool, enum.Enum))


def _enc(x, depth=0):
    """A hashable stand-in for a simple value (turned into JSON when written), or "?" otherwise."""
    if x is None or isinstance(x, (bool, int)):
        return x
    if isinstance(x, float):
        return x if x == x else "?"
    if isinstance(x, str):
        return x if len(x) <= 64 else "?"
    if isinstance(x, enum.Enum):
        return ("enum", type(x).__qualname__ + "." + x.name)
    if isinstance(x, tuple) and depth < 2 and len(x) <= 8:
        items = tuple(_enc(y, depth + 1) for y in x)
        return "?" if "?" in items else ("tuple", items)
    return "?"


def _json(x):
    if isinstance(x, tuple) and len(x) == 2 and x[0] == "enum":
        return {"enum": x[1]}
    if isinstance(x, tuple) and len(x) == 2 and x[0] == "tuple":
        return {"tuple": [_json(y) for y in x[1]]}
    return x


_BUSY = [False]


def _cmp(site, op, a, b):
    r = _OPS[op](a, b)
    if _BUSY[0]:
        return r                    # comparisons made while the profiler evaluates its own a == b
    _BUSY[0] = True
    try:
        _record(site, op, a, b, r)
    finally:
        _BUSY[0] = False
    return r


def _record(site, op, a, b, r):
    ctx = brhooks.CUR[0] or "IMPORT"
    if brhooks.DEFINING[0]:
        ctx = "IMPORT"
    eq = None                       # unknown unless both sides are simple values
    if op in ("Eq", "NotEq"):
        if isinstance(r, bool):
            eq = r if op == "Eq" else not r
    elif (_simple(a) and _simple(b)) or _singleton(a) or _singleton(b):
        try:
            eq = bool(a == b)
        except Exception:
            eq = None
    v = ((a is b), eq, _enc(a), _enc(b))
    s = PROF.get((site, ctx))
    if s is None:
        s = PROF[(site, ctx)] = set()
    if len(s) < 48:
        s.add(v)
    else:
        s.add((v[0], v[1], "?", "?"))
    return r


class _T(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.depth = 0

    def _fn(self, node):
        # decorators, defaults and annotations run at definition time: leave them alone
        self.depth += 1
        node.body = [self.visit(s) for s in node.body]
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _fn

    def visit_Lambda(self, node):
        self.depth += 1
        node.body = self.visit(node.body)
        self.depth -= 1
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.depth == 0 or len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in _OPS:
            return node
        site = "%s:%d:%d" % (self.rel, node.lineno, node.col_offset)
        SITES.add(site)
        call = ast.Call(func=ast.Name(id="_brcmp_", ctx=ast.Load()),
                        args=[ast.Constant(site), ast.Constant(op), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


def _rel(path):
    i = path.rfind(os.sep + "sqlglot" + os.sep)
    if i < 0 or (os.sep + "tests" + os.sep) in path[i:]:
        return None
    return path[i + 1:].replace(os.sep, "/")


def install():
    builtins._brcmp_ = _cmp
    orig = SourceFileLoader.get_code

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        rel = _rel(path) if path.endswith(".py") else None
        if rel is None:
            return orig(self, fullname)
        src = self.get_data(path)
        tree = ast.parse(src, filename=path)
        tree = _T(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    SourceFileLoader.get_code = get_code


_SITES_WRITTEN = set()


def dump(path):
    new_sites = SITES - _SITES_WRITTEN
    if not PROF and not new_sites:
        return
    _SITES_WRITTEN.update(new_sites)
    with open(path, "a") as f:
        out = {}
        for (site, ctx), vals in PROF.items():
            out.setdefault(site, {})[ctx] = sorted(json.dumps([v[0], v[1], _json(v[2]), _json(v[3])]) for v in vals)
        f.write(json.dumps({"sites": sorted(new_sites), "prof": out}) + "\n")
    PROF.clear()
