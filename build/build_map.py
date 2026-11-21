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
sets, setlist, files = {}, [], {}
for f in cd.measured_files():
    rel = os.path.relpath(f, root)
    lines = collections.defaultdict(set)
    for line, ctxs in cd.contexts_by_lineno(f).items():
        for c in ctxs:
            if c == '':
                lines[line].add(-1)          # import time / collection time
            else:
                lines[line].update(expand(c))
    out_f = {}
    for line, s in lines.items():
        k = frozenset(s)
        if k not in sets:
            sets[k] = len(setlist); setlist.append(sorted(k))
        out_f[str(line)] = sets[k]
    files[rel] = out_f
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
json.dump({'ids': ids, 'sets': setlist, 'files': files, 'classes': classes, 'opens': opens, 'refs': refs},
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'map.json'), 'w'), separators=(',', ':'))
