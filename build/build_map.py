"""Builds data/map.json from the coverage run of the pinned sqlglot test suite.

Inputs (written by build/covplugin.py during `coverage run ... pytest`):
  cov.db, opens.json, classes.json.  tests.json supplies the test ids.
"""
import collections, json, os, re, sys
import coverage

work, root, tests_json, out = sys.argv[1:5]
tests = json.load(open(tests_json))['tests']
ids = [t['id'] for t in tests]
num = {t: i for i, t in enumerate(ids)}
cd = coverage.CoverageData(os.path.join(work, 'cov.db')); cd.read()
def expand(c):
    """SETUP:<scope> contexts belong to every test in that scope."""
    if c.startswith('SETUP:'):
        k = c[6:]
        if k == 'ALL':
            return range(len(ids))
        return [i for i, t in enumerate(ids) if t.startswith(k + '::')]
    return [num[c]] if c in num else []
sets, setlist, files, files_imp = {}, [], {}, {}
def sid(s):
    k = frozenset(s)
    if k not in sets:
        sets[k] = len(setlist); setlist.append(sorted(k))
    return sets[k]
for f in cd.measured_files():
    rel = os.path.relpath(f, root)
    lines = collections.defaultdict(set)
    imp = collections.defaultdict(set)
    for line, ctxs in cd.contexts_by_lineno(f).items():
        for c in ctxs:
            if c == '':
                lines[line].add(-1)          # import time / collection time, no test running
            elif c.startswith('IMP:'):
                imp[line].update(expand(c[4:]))   # run while a test triggered this module's import
            else:
                lines[line].update(expand(c))
    files[rel] = {str(l): sid(s) for l, s in lines.items()}
    files_imp[rel] = {str(l): sid(s) for l, s in imp.items()}
classes = collections.defaultdict(list)
for t, cl in json.load(open(os.path.join(work, 'classes.json'))).items():
    for i in expand(t):
        for c in cl: classes[c].append(i)
import glob
for fn in glob.glob(os.path.join(work, 'classes.*.jsonl')):
    for line in open(fn):
        t, cl = json.loads(line)
        for i in expand(t):
            for c in cl: classes[c].append(i)
for c in classes: classes[c] = sorted(set(classes[c]))
opens = collections.defaultdict(list)
for t, fl in json.load(open(os.path.join(work, 'opens.json'))).items():
    for i in expand(t):
        for f in fl: opens[f].append(i)
# text references: fixture file -> test files that mention its path or base name
refs = {}
fx = []
for dp, _, fns in os.walk(os.path.join(root, 'tests', 'fixtures')):
    for fn in fns: fx.append(os.path.relpath(os.path.join(dp, fn), root))
tsrc = {}
for dp, _, fns in os.walk(os.path.join(root, 'tests')):
    for fn in fns:
        if fn.endswith('.py'):
            p = os.path.join(dp, fn); tsrc[os.path.relpath(p, root)] = open(p, encoding='utf-8').read()
for f in fx:
    base = os.path.basename(f); stem = base.rsplit('.', 1)[0]
    sub = f[len('tests/fixtures/'):]
    refs[f] = sorted(p for p, s in tsrc.items() if sub in s or base in s or ('"%s"' % stem in s or "'%s'" % stem in s))
# class attribute reads: name -> owner class -> set id; plus owners read at import time
attr, attr_fn, attr_body, mro = {}, {}, {}, {}
acc = collections.defaultdict(set)
for fn in glob.glob(os.path.join(work, 'attrs.*.jsonl')):
    for line in open(fn):
        r = json.loads(line)
        if r[0] == '__MRO__':
            mro.update(r[1]); continue
        k, n, ctxs = r
        for c in ctxs:
            if c.startswith('IMPORT_FN'):
                attr_fn.setdefault(n, {}).setdefault(c[len('IMPORT_FN:'):], set()).add(k)
            elif c == 'IMPORT_BODY':
                attr_body.setdefault(n, set()).add(k)
            else:
                acc[(n, k)].update(expand(c))
for (n, k), s in acc.items():
    fs = frozenset(s)
    if fs not in sets:
        sets[fs] = len(setlist); setlist.append(sorted(fs))
    attr.setdefault(n, {})[k] = sets[fs]
attr_fn = {n: {f: sorted(o) for f, o in v.items()} for n, v in attr_fn.items()}
attr_body = {n: sorted(v) for n, v in attr_body.items()}
json.dump({'ids': ids, 'sets': setlist, 'files': files, 'classes': classes, 'opens': opens, 'refs': refs,
           'files_imp': files_imp, 'attr': attr, 'attr_fn': attr_fn, 'attr_body': attr_body, 'mro': mro},
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'map.json'), 'w'), separators=(',', ':'))

# ---- static index: import-time (module / class body) references to attribute names ----
import ast as _ast
names_of_interest = set(attr) | set(attr_fn) | set(attr_body)
body_refs = collections.defaultdict(list)   # name -> [[file, class chain, targets, base, kind, line]]

def _base_name(node):
    v = node.value
    while isinstance(v, _ast.Attribute):
        return v.attr
    if isinstance(v, _ast.Name):
        return v.id
    return None

def _visit(nodes, path, chain, in_fn, stmt_info):
    for n in nodes:
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            info = (n, [])
            for d in n.decorator_list:
                _scan_expr(d, path, chain, in_fn, info)
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                _scan_expr(d, path, chain, in_fn, info)
            _visit(n.body, path, chain, True, None)
        elif isinstance(n, _ast.ClassDef):
            info = (n, [])
            for b in n.bases + [k.value for k in n.keywords] + n.decorator_list:
                _scan_expr(b, path, chain, in_fn, info)
            _visit(n.body, path, chain + [n.name], in_fn, None)
        elif isinstance(n, _ast.stmt):
            targets = []
            if isinstance(n, _ast.Assign):
                targets = [t.id if isinstance(t, _ast.Name) else None for t in n.targets]
            elif isinstance(n, (_ast.AnnAssign, _ast.AugAssign)):
                targets = [n.target.id if isinstance(n.target, _ast.Name) else None]
            info = (n, targets)
            simple = (_ast.Assign, _ast.AnnAssign, _ast.AugAssign, _ast.Expr, _ast.Return, _ast.Import,
                      _ast.ImportFrom, _ast.Pass, _ast.Raise, _ast.Assert, _ast.Delete, _ast.Global,
                      _ast.Nonlocal, _ast.Break, _ast.Continue)
            if isinstance(n, simple):
                _scan_expr(n, path, chain, in_fn, info)
            else:
                for fld, val in _ast.iter_fields(n):
                    if isinstance(val, list) and val and isinstance(val[0], _ast.stmt):
                        _visit(val, path, chain, in_fn, None)
                    elif isinstance(val, list):
                        for v in val:
                            if isinstance(v, _ast.ExceptHandler):
                                _visit(v.body, path, chain, in_fn, None)
                            elif isinstance(v, _ast.match_case):
                                _visit(v.body, path, chain, in_fn, None)
                            elif isinstance(v, _ast.AST):
                                _scan_expr(v, path, chain, in_fn, info)
                    elif isinstance(val, _ast.AST):
                        _scan_expr(val, path, chain, in_fn, info)

def _scan_expr(node, path, chain, in_fn, info):
    if in_fn:
        return
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, _ast.Lambda):
            continue            # body runs later, at call time: covered by recorded reads
        if isinstance(x, _ast.Attribute) and x.attr in names_of_interest:
            body_refs[x.attr].append([path, ".".join(chain), info[1], _base_name(x), "attr", x.lineno])
        elif isinstance(x, _ast.Name) and x.id in names_of_interest and isinstance(x.ctx, _ast.Load):
            body_refs[x.id].append([path, ".".join(chain), info[1], None, "bare", x.lineno])
        elif isinstance(x, _ast.Constant) and isinstance(x.value, str) and x.value in names_of_interest:
            body_refs[x.value].append([path, ".".join(chain), info[1], None, "string", x.lineno])
        stack.extend(_ast.iter_child_nodes(x))

for dp, _, fns in os.walk(os.path.join(root, 'sqlglot')):
    for fn in fns:
        if fn.endswith('.py'):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root)
            _visit(_ast.parse(open(p, encoding='utf-8').read()).body, rel, [], False, None)

outp = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'map.json')
mm = json.load(open(outp))
mm['body_refs'] = body_refs
json.dump(mm, open(outp, 'w'), separators=(',', ':'))

# ---- names read by code that runs while a class is created (metaclass __new__/__init__,
# __init_subclass__, __set_name__): a class's own attributes are read there before any
# recording is possible, and what they derive outlives the test.
creation_names = set()
for dp, _, fns in os.walk(os.path.join(root, 'sqlglot')):
    for fn in fns:
        if not fn.endswith('.py'):
            continue
        tree = _ast.parse(open(os.path.join(dp, fn), encoding='utf-8').read())
        hooks = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                meta = any(isinstance(b, _ast.Name) and b.id == 'type' for b in node.bases)
                for f in node.body:
                    if isinstance(f, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and (
                            f.name in ('__init_subclass__', '__set_name__')
                            or (meta and f.name in ('__new__', '__init__'))):
                        hooks.append(f)
        for f in hooks:
            for x in _ast.walk(f):
                if isinstance(x, _ast.Attribute):
                    creation_names.add(x.attr)
                elif isinstance(x, _ast.Constant) and isinstance(x.value, str) and x.value.isidentifier():
                    creation_names.add(x.value)
mm = json.load(open(outp))
mm['creation_names'] = sorted(creation_names)
json.dump(mm, open(outp, 'w'), separators=(',', ':'))


# ---- class-creation hooks: what each reads and what it writes, and where it applies
MUTATORS = {'pop', 'setdefault', 'update', 'clear', 'add', 'remove', 'discard', 'append', 'extend',
            'insert', 'popitem', '__setitem__', '__delitem__'}

def _hook_flow(f):
    """Which attribute names each written attribute depends on, inside one hook function.
    Simple taint: locals carry the attribute reads they were computed from, conditions taint
    everything they guard.  'opaque' collects reads in statements we do not model (raise,
    assert, return, bare calls), whose effect on class creation is unknown."""
    var = collections.defaultdict(set)
    deps = collections.defaultdict(set)
    opaque = set()

    def rd(node):
        out = set()
        if node is None:
            return out
        for x in _ast.walk(node):
            if isinstance(x, _ast.Attribute) and isinstance(x.ctx, _ast.Load):
                out.add(x.attr)
            elif isinstance(x, _ast.Name) and isinstance(x.ctx, _ast.Load):
                out |= var.get(x.id, set())
            elif isinstance(x, _ast.Constant) and isinstance(x.value, str) and x.value.isidentifier():
                out.add(x.value)
        return out

    def assign(t, r):
        if isinstance(t, _ast.Name):
            var[t.id] |= r
        elif isinstance(t, _ast.Attribute):
            deps[t.attr] |= r | rd(t.value)
        elif isinstance(t, _ast.Subscript):
            if isinstance(t.value, _ast.Attribute):
                deps[t.value.attr] |= r | rd(t.value.value) | rd(t.slice)
            elif isinstance(t.value, _ast.Name):
                var[t.value.id] |= r | rd(t.slice)
            else:
                opaque.update(r)
        elif isinstance(t, (_ast.Tuple, _ast.List)):
            for e in t.elts:
                assign(e, r)
        elif isinstance(t, _ast.Starred):
            assign(t.value, r)
        else:
            opaque.update(r)

    def block(stmts, ctl):
        for s in stmts:
            if isinstance(s, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda)):
                var[s.name] |= rd(s) | ctl
            elif isinstance(s, _ast.Assign):
                r = rd(s.value) | ctl
                for t in s.targets:
                    assign(t, r)
            elif isinstance(s, (_ast.AugAssign, _ast.AnnAssign)):
                r = rd(s.value) | ctl | rd(s.target)
                assign(s.target, r)
            elif isinstance(s, _ast.Expr) and isinstance(s.value, _ast.Call):
                c = s.value
                r = ctl | set().union(*[rd(a) for a in c.args], *[rd(k.value) for k in c.keywords])
                if isinstance(c.func, _ast.Attribute) and c.func.attr in MUTATORS and isinstance(c.func.value, _ast.Attribute):
                    deps[c.func.value.attr] |= r | rd(c.func.value.value)
                elif isinstance(c.func, _ast.Attribute) and c.func.attr in MUTATORS and isinstance(c.func.value, _ast.Name):
                    var[c.func.value.id] |= r
                elif isinstance(c.func, _ast.Name) and c.func.id == 'setattr' and len(c.args) == 3 \
                        and isinstance(c.args[1], _ast.Constant):
                    deps[c.args[1].value] |= ctl | rd(c.args[2]) | rd(c.args[0])
                elif isinstance(c.func, _ast.Attribute) and c.func.attr == '__init_subclass__':
                    pass                        # super().__init_subclass__(**kwargs)
                else:
                    opaque.update(rd(c) | ctl)
            elif isinstance(s, (_ast.If, _ast.While)):
                c2 = ctl | rd(s.test)
                block(s.body, c2); block(s.orelse, c2)
            elif isinstance(s, (_ast.For, _ast.AsyncFor)):
                r = rd(s.iter) | ctl
                assign(s.target, r)
                block(s.body, r); block(s.orelse, r)
            elif isinstance(s, (_ast.With, _ast.AsyncWith)):
                r = ctl | set().union(*[rd(i.context_expr) for i in s.items])
                block(s.body, r)
            elif isinstance(s, _ast.Try):
                block(s.body, ctl); block(s.orelse, ctl); block(s.finalbody, ctl)
                for h in s.handlers:
                    block(h.body, ctl)
            elif isinstance(s, (_ast.Pass, _ast.Expr)) and not (isinstance(s, _ast.Expr) and not isinstance(s.value, _ast.Constant)):
                pass
            elif isinstance(s, _ast.Return) and isinstance(s.value, _ast.Name):
                pass                            # return klass
            else:
                opaque.update(rd(s) | ctl)

    for _ in range(3):                          # loops: let locals settle
        block(f.body, set())
    return deps, opaque

hooks = []
for dp, _, fns in os.walk(os.path.join(root, 'sqlglot')):
    for fn in fns:
        if not fn.endswith('.py'):
            continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        tree = _ast.parse(open(os.path.join(dp, fn), encoding='utf-8').read())
        for node in tree.body:
            if not isinstance(node, _ast.ClassDef):
                continue
            meta = any(isinstance(b, _ast.Name) and b.id == 'type' for b in node.bases)
            for f in node.body:
                if not isinstance(f, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                if f.name in ('__init_subclass__', '__set_name__'):
                    kind = 'self'
                elif meta and f.name in ('__new__', '__init__'):
                    kind = 'dialect' if node.name == '_Dialect' else 'unknown'
                else:
                    continue
                deps, opaque = _hook_flow(f)
                reads, writes = set(), set()
                for x in _ast.walk(f):
                    if isinstance(x, _ast.Attribute):
                        (writes if isinstance(x.ctx, (_ast.Store, _ast.Del)) else reads).add(x.attr)
                    elif isinstance(x, _ast.Call) and isinstance(x.func, _ast.Attribute) and x.func.attr in MUTATORS \
                            and isinstance(x.func.value, _ast.Attribute):
                        writes.add(x.func.value.attr)
                    elif isinstance(x, (_ast.Assign, _ast.AugAssign, _ast.Delete)):
                        for t in (x.targets if isinstance(x, (_ast.Assign, _ast.Delete)) else [x.target]):
                            if isinstance(t, _ast.Subscript) and isinstance(t.value, _ast.Attribute):
                                writes.add(t.value.attr)
                    elif isinstance(x, _ast.Call) and isinstance(x.func, _ast.Name) and x.func.id in ('setattr', 'getattr', 'delattr') \
                            and len(x.args) > 1 and isinstance(x.args[1], _ast.Constant) and isinstance(x.args[1].value, str):
                        (reads if x.func.id == 'getattr' else writes).add(x.args[1].value)
                    elif isinstance(x, _ast.Constant) and isinstance(x.value, str) and x.value.isidentifier():
                        reads.add(x.value)   # e.g. klass.__dict__.get("Parser")
                owner = module_of_rel = rel[:-3].replace('/', '.')
                if owner.endswith('.__init__'):
                    owner = owner[:-9]
                hooks.append({'kind': kind, 'file': fn, 'qual': node.name + '.' + f.name,
                              'cls': owner + ':' + node.name, 'reads': sorted(reads), 'writes': sorted(writes),
                              'deps': {w: sorted(r) for w, r in deps.items()}, 'opaque': sorted(opaque)})
comp_path = os.path.join(work, 'components.json')
mm = json.load(open(outp))
mm['hooks'] = hooks
mm['components'] = json.load(open(comp_path)) if os.path.exists(comp_path) else {}
json.dump(mm, open(outp, 'w'), separators=(',', ':'))


# ---- comparison profile (optional: BR_PROFILE run outputs prof.*.jsonl in its work dir) ----
prof_dir = os.environ.get('BR_PROF_DIR')
if prof_dir:
    cmp_sites = set()
    acc = collections.defaultdict(lambda: collections.defaultdict(set))
    imp = collections.defaultdict(set)
    for fn in glob.glob(os.path.join(prof_dir, 'prof.*.jsonl')):
        for line in open(fn):
            r = json.loads(line)
            cmp_sites.update(r['sites'])
            for site, ctxs in r['prof'].items():
                for ctx, vals in ctxs.items():
                    for v in vals:
                        if ctx == 'IMPORT':
                            imp[site].add(v)
                        else:
                            acc[site][v].update(expand(ctx))
    mm = json.load(open(outp))
    mm['cmp_sites'] = sorted(cmp_sites)
    mm['cmp'] = {s: {v: sid(t) for v, t in d.items()} for s, d in acc.items()}
    mm['cmp']  # noqa
    mm['sets'] = setlist
    mm['cmp_import'] = {s: sorted(v) for s, v in imp.items()}
    json.dump(mm, open(outp, 'w'), separators=(',', ':'))
