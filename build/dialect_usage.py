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
    # lookups by name through the metaclass: Dialect["teradata"], Dialect.get(...) on subclasses
    meta = type(Dialect)
    mgi = meta.__getitem__
    def getitem(cls, key):      # mgi is bound (a classmethod of the metaclass)
        r = mgi(key)
        _note(r)
        return r
    meta.__getitem__ = getitem
    if "get" in vars(meta):
        mg = meta.get
        def mget(cls, *a, **k):
            r = mg(*a, **k)
            _note(r)
            return r
        meta.get = mget
    if isinstance(vars(meta).get("classes"), property):
        mc = vars(meta)["classes"].fget
        def classes(cls):
            r = mc(cls)
            for v in (r.values() if isinstance(r, dict) else ()):
                _note(v)
            return r
        meta.classes = property(classes)

def pytest_runtest_setup(item):
    _cur[0] = item.nodeid

def pytest_unconfigure(config):
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    json.dump({k: sorted(v) for k, v in _rec.items()}, open(f"/home/claude/work/dplug/usage_{w}.json", "w"))
