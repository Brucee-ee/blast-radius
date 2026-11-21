"""Early hooks (loaded from sitecustomize before sqlglot is imported) that record which test
reads which class attribute of which sqlglot class.

Every plain class attribute (not callable, not a descriptor, not a dunder) of every class
defined in a sqlglot module is replaced right after the class is created by a recording
descriptor.  Reads while a module is imported or a class is created are labelled
IMPORT_BODY (class-body / module-level code inside the sqlglot package) or IMPORT_FN
(anything else), because their results outlive the test that triggered them."""
import builtins
import enum
import importlib
import os
import sys

CUR = [None]            # current test node id, set by covplugin
DEFINING = [0]          # >0 while importing a module or creating a class
ATTR_READS = {}         # ctx -> set((owner_cls, name))
ATTR_DONE = set()
_SQLGLOT_DIR = os.sep + "sqlglot" + os.sep
_TESTS_DIR = os.sep + "tests" + os.sep


def _import_kind():
    f = sys._getframe(2)
    if f.f_code.co_flags & 1:   # CO_OPTIMIZED: a function
        return "IMPORT_FN"
    fn = f.f_code.co_filename
    return "IMPORT_BODY" if _SQLGLOT_DIR in fn and _TESTS_DIR not in fn else "IMPORT_FN"


class _Rec:
    __slots__ = ("v", "n")

    def __init__(self, v, n):
        self.v = v
        self.n = n

    def __get__(self, obj, owner):
        c = CUR[0]
        if c is None or DEFINING[0]:
            c = _import_kind()
        s = ATTR_READS.get(c)
        if s is None:
            s = ATTR_READS[c] = set()
        s.add((owner, self.n))
        return self.v


def wrap_cls(cls):
    if cls in ATTR_DONE:
        return
    ATTR_DONE.add(cls)
    try:
        if issubclass(cls, enum.Enum):
            return
    except TypeError:
        return
    for name, val in list(cls.__dict__.items()):
        if name.startswith("__") or isinstance(val, _Rec) or hasattr(val, "__get__"):
            continue
        try:
            setattr(cls, name, _Rec(val, name))
        except (AttributeError, TypeError):
            pass


def _defining(fn):
    def w(*a, **k):
        DEFINING[0] += 1
        try:
            return fn(*a, **k)
        finally:
            DEFINING[0] -= 1
    w._br = True
    return w


def install_early():
    if getattr(builtins.__build_class__, "_br", False):
        return
    orig_bc = builtins.__build_class__

    def bc(func, name, *bases, **kw):
        DEFINING[0] += 1
        try:
            cls = orig_bc(func, name, *bases, **kw)
        finally:
            DEFINING[0] -= 1
        mod = getattr(cls, "__module__", "") or ""
        if isinstance(cls, type) and (mod == "sqlglot" or mod.startswith("sqlglot.")):
            wrap_cls(cls)
        return cls

    bc._br = True
    builtins.__build_class__ = bc
    builtins.__import__ = _defining(builtins.__import__)
    importlib.import_module = _defining(importlib.import_module)
