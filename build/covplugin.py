"""pytest plugin: per-test line coverage contexts + fixture file opens."""
import json, os, sys
import coverage
import pytest

OPENS = {}
CLASSES = {}

CUR = [None]
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
            _install(); _install_expr()
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
    CUR[0] = item.nodeid
    if cov:
        cov.switch_context(item.nodeid)
    yield
    CUR[0] = None
    if cov:
        cov.switch_context("")

def pytest_sessionfinish(session):
    out = os.environ.get("OPENS_OUT")
    if out:
        json.dump({k: sorted(v) for k, v in OPENS.items()}, open(out, "w"))
        json.dump({k: sorted(v) for k, v in CLASSES.items()}, open(out.replace("opens", "classes"), "w"))
