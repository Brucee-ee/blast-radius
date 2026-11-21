"""Splits data/map.json into the gzipped files committed to the repository:
data/map.json.gz (everything but the comparison profile) and data/cmp.json.gz (the profile,
loaded only when a patch edits a comparison).  The Dockerfile decompresses both."""
import gzip, json, os, sys
d = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
m = json.load(open(os.path.join(d, 'map.json')))
cmp = {k: m.pop(k) for k in ('cmp_sites', 'cmp', 'cmp_import') if k in m}
for name, obj in (('map.json.gz', m), ('cmp.json.gz', cmp)):
    with gzip.open(os.path.join(d, name), 'wt', compresslevel=9) as f:
        json.dump(obj, f, separators=(',', ':'))
os.remove(os.path.join(d, 'map.json'))
