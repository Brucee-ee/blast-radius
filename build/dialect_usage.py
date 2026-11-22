import os, json, atexit
_cur = [None]
_rec = {}
def _note(cls):
    t = _cur[0]
    if t is None or cls is None:
        return
    s = _rec.setdefault(t, set())
    for k in getattr(cls, "__mro__", ()):
        mod = getattr(k, "__module__", "")
        if mod.startswith("sqlglot.dialects."):
            s.add(mod)

def pytest_configure(config):
    from sqlglot.dialects.dialect import Dialect
    oi = Dialect.__init__
    def init(self, *a, **k):
        _note(type(self))
        return oi(self, *a, **k)
    Dialect.__init__ = init
    og = Dialect.get.__func__
    def get(cls, *a, **k):
        r = og(cls, *a, **k)
        _note(r)
        return r
    Dialect.get = classmethod(get)
    ogr = Dialect.get_or_raise.__func__
    def gor(cls, *a, **k):
        r = ogr(cls, *a, **k)
        _note(type(r) if not isinstance(r, type) else r)
        return r
    Dialect.get_or_raise = classmethod(gor)

def pytest_runtest_setup(item):
    _cur[0] = item.nodeid

def pytest_unconfigure(config):
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    json.dump({k: sorted(v) for k, v in _rec.items()}, open(f"/home/claude/work/dplug/usage_{w}.json", "w"))
