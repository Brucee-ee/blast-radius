"""pytest plugin: per-test line coverage contexts + fixture file opens."""
import json, os, sys
import coverage
import pytest

OPENS = {}
CLASSES = {}
import brhooks
from brhooks import CUR, DEFINING, ATTR_READS, ATTR_DONE, _Rec

ROOT = os.getcwd()

def _hook(event, args):
    if event == "open" and CUR[0] is not None:
        p = args[0]
        if isinstance(p, str) and "/tests/" in p or (isinstance(p, str) and p.startswith("tests/")):
            OPENS.setdefault(CUR[0], set()).add(os.path.relpath(os.path.abspath(p), ROOT))

sys.addaudithook(_hook)

def _rec(cls):
    t = CUR[0]
    if t is None or not isinstance(cls, type):
        return
    s = CLASSES.setdefault(t, set())
    for c in (cls, getattr(cls, "parser_class", None), getattr(cls, "generator_class", None),
              getattr(cls, "tokenizer_class", None), getattr(cls, "jsonpath_tokenizer_class", None)):
        if isinstance(c, type):
            for k in c.__mro__:
                if k.__module__.startswith("sqlglot"):
                    s.add(k.__module__ + ":" + k.__qualname__)

def _install():
    from sqlglot.dialects import dialect as d
    orig_call = d._Dialect.__call__
    def call(cls, *a, **k):
        _rec(cls)
        return orig_call(cls, *a, **k)
    d._Dialect.__call__ = call
    for name in ("get_or_raise",):
        orig = getattr(d.Dialect, name).__func__
        def mk(orig):
            def w(cls, *a, **k):
                r = orig(cls, *a, **k)
                _rec(r if isinstance(r, type) else type(r))
                return r
            return classmethod(w)
        setattr(d.Dialect, name, mk(orig))
    og = getattr(d._Dialect, "get", None) or getattr(d.Dialect, "get", None)
    if og is not None and isinstance(d.Dialect.__dict__.get("get"), classmethod):
        f = d.Dialect.__dict__["get"].__func__
        def w2(cls, *a, **k):
            r = f(cls, *a, **k)
            if r is not None: _rec(r)
            return r
        d.Dialect.get = classmethod(w2)

_CHILD_READY = [False]

def run_with_ctx(nodeid, blob):
    """Runs in a pool worker: label coverage/class records with the submitting test.
    Arguments arrive pickled and the result leaves pickled, so that (de)serialisation
    happens in the worker's main thread and in the submitting test's thread."""
    import pickle
    cov = coverage.Coverage.current()
    if not _CHILD_READY[0]:
        _CHILD_READY[0] = True
        try:
            _install(); _install_expr(); _install_attrs()
        except Exception:
            pass
    if cov:
        cov.switch_context(nodeid)
    CUR[0] = nodeid
    try:
        fn, a, k = pickle.loads(blob)
        return pickle.dumps(fn(*a, **k))
    finally:
        CUR[0] = None
        if cov:
            cov.switch_context("")
        _dump_attr(os.path.join(os.environ.get("COV_CLASSES_DIR", "/tmp"), "attrs.%d.jsonl" % os.getpid()))
        s = CLASSES.pop(nodeid, None)
        if s:
            with open(os.path.join(os.environ.get("COV_CLASSES_DIR", "/tmp"), "classes.%d.jsonl" % os.getpid()), "a") as f:
                f.write(json.dumps([nodeid, sorted(s)]) + "\n")

def _install_pool():
    import pickle
    from concurrent.futures import process as cp
    orig = cp.ProcessPoolExecutor.submit
    def submit(self, fn, *a, **k):
        if CUR[0] is None:
            return orig(self, fn, *a, **k)
        fut = orig(self, run_with_ctx, CUR[0], pickle.dumps((fn, a, k)))
        r = fut.result
        fut.result = lambda timeout=None: pickle.loads(r(timeout))
        return fut
    cp.ProcessPoolExecutor.submit = submit


# ---------------------------------------------------------------- class attribute reads
# Every plain (non-callable, non-dunder) class attribute of sqlglot's Dialect, Parser,
# Generator, Tokenizer and Expr hierarchies is wrapped in a recording descriptor, so we
# learn which tests read which attribute through which class.  Reads with no test
# running (imports, collection) are recorded under "IMPORT".
ATTR_PAIRS = {}          # (owner_key, name) -> set(ctx)
ATTR_MRO = {}            # owner_key -> [mro keys]
_wrap_cls = brhooks.wrap_cls

def _bases():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import _TokenizerBase
    from sqlglot.expressions.core import Expr
    return (Dialect, Parser, Generator, _TokenizerBase, Expr)

def _wrap_tree(base):
    stack = [base]
    while stack:
        c = stack.pop()
        _wrap_cls(c)
        stack.extend(c.__subclasses__())

def _wrap_all():
    for b in _bases():
        _wrap_tree(b)

def _key(c):
    return c.__module__ + ":" + c.__qualname__

def _flush_attr_reads():
    for ctx, s in ATTR_READS.items():
        for owner, name in s:
            k = _key(owner)
            if k not in ATTR_MRO:
                ATTR_MRO[k] = [_key(x) for x in owner.__mro__ if x is not object]
            ATTR_PAIRS.setdefault((k, name), set()).add(ctx)
    ATTR_READS.clear()

def _install_attrs():
    from sqlglot import generator as g
    from sqlglot.dialects import dialect as d
    from sqlglot.expressions import core
    from sqlglot import tokens

    class _NoCache(dict):
        def get(self, *a, **k):
            return None
        def __setitem__(self, k, v):
            pass
    g._DISPATCH_CACHE = _NoCache()   # make every generator build its dispatch (reads TRANSFORMS)

    brhooks.install_early()
    orig_new = d._Dialect.__new__
    def new(mcls, *a, **k):
        DEFINING[0] += 1
        try:
            klass = orig_new(mcls, *a, **k)
        finally:
            DEFINING[0] -= 1
        for c in (klass, getattr(klass, "parser_class", None), getattr(klass, "generator_class", None),
                  getattr(klass, "tokenizer_class", None), getattr(klass, "jsonpath_tokenizer_class", None)):
            if isinstance(c, type):
                for x in c.__mro__:
                    _wrap_cls(x)
        return klass
    d._Dialect.__new__ = new

    for base in (core.Expr, tokens._TokenizerBase):
        orig = base.__dict__["__init_subclass__"].__func__
        def mk(orig):
            def sub(cls, **kw):
                DEFINING[0] += 1
                try:
                    orig(cls, **kw)
                finally:
                    DEFINING[0] -= 1
                _wrap_cls(cls)
            return classmethod(sub)
        base.__init_subclass__ = mk(orig)
    _wrap_all()

_WRITTEN = set()
_MRO_WRITTEN = set()

def _dump_attr(path):
    _flush_attr_reads()
    lines = []
    for (k, n), ctxs in ATTR_PAIRS.items():
        new = [c for c in ctxs if (k, n, c) not in _WRITTEN]
        if new:
            _WRITTEN.update((k, n, c) for c in new)
            lines.append(json.dumps([k, n, sorted(new)]))
    mro = {k: v for k, v in ATTR_MRO.items() if k not in _MRO_WRITTEN}
    if mro:
        _MRO_WRITTEN.update(mro)
        lines.append(json.dumps(["__MRO__", mro]))
    if lines:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    ATTR_PAIRS.clear()

def _install_expr():
    from sqlglot.expressions import core
    E = core.Expr
    orig = E.__init__
    def init(self, *a, **k):
        t = CUR[0]
        if t is not None:
            s = CLASSES.setdefault(t, set())
            for c in type(self).__mro__:
                m = c.__module__
                if m.startswith("sqlglot"):
                    key = m + ":" + c.__qualname__
                    if key in s:
                        break
                    s.add(key)
        orig(self, *a, **k)
    E.__init__ = init

def pytest_sessionstart(session):
    _install()
    _install_expr()
    _install_pool()
    _install_attrs()

@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Shared setup (setUpClass, module/session fixtures) runs inside one test but serves many:
    record it under a SETUP:<scope node> context that the map expands to every test in that scope."""
    cur = CUR[0]
    scope = fixturedef.scope
    key = None
    if cur is not None and scope in ("class", "module", "package", "session"):
        if scope == "class":
            key = "SETUP:" + cur.rsplit("::", 1)[0]
        elif scope == "module":
            key = "SETUP:" + cur.split("::", 1)[0]
        else:
            key = "SETUP:ALL"
    cov = coverage.Coverage.current()
    if key:
        CUR[0] = key
        if cov:
            cov.switch_context(key)
    try:
        yield
    finally:
        if key:
            CUR[0] = cur
            if cov:
                cov.switch_context(cur)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cov = coverage.Coverage.current()
    try:
        _wrap_all()
    except Exception:
        pass
    CUR[0] = item.nodeid
    if cov:
        cov.switch_context(item.nodeid)
    yield
    _flush_attr_reads()
    CUR[0] = None
    if cov:
        cov.switch_context("")

def pytest_sessionfinish(session):
    _dump_attr(os.path.join(os.environ.get("COV_CLASSES_DIR", "/tmp"), "attrs.main.jsonl"))
    out = os.environ.get("OPENS_OUT")
    if out:
        json.dump({k: sorted(v) for k, v in OPENS.items()}, open(out, "w"))
        json.dump({k: sorted(v) for k, v in CLASSES.items()}, open(out.replace("opens", "classes"), "w"))
