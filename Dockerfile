# Stage 1: build data/tpc.pkl for the TPC-DS check (tpgate.py, tpcheck.py) from sqlglot at the
# pinned commit: its TPC-DS queries, a sample of the TPC-DS fixture tables, fingerprints of the
# pinned sqlglot's pipeline output, and per-query line coverage (build/tpc_all.sh).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS tpc
RUN pip install --no-cache-dir coverage==7.17.4 duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/', filter='data')" \
 && mv /sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /sqlglot
COPY tpcheck.py /b/tpcheck.py
COPY build/tpc_build.py build/tpc_lines.py build/tpc_runtime.py build/tpc_all.sh /b/build/
RUN sh /b/build/tpc_all.sh /sqlglot

# Stage 2: the selector image.
# Same base image the labels were made on (its Python and SQLite decide which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector uses only the standard library. data/*.gz hold maps built from the pinned
# sqlglot commit (see build/); src.tgz is that commit's sqlglot/ and tests/ python files, read
# by the selector for AST analysis and, for the TPC-DS check, copied to /tmp and patched.
COPY selector.py tpgate.py tpcheck.py /app/
COPY data /app/data
COPY src.tgz /app/src.tgz
COPY --from=tpc /b/tpc.pkl.gz /b/tpc_full.pkl /app/data/
COPY --from=tpc /sqlglot/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql /app/data/tpc-ds.sql
RUN python -c "import gzip, shutil, tarfile; \
[shutil.copyfileobj(gzip.open('/app/data/%s.gz' % n), open('/app/data/%s' % n, 'wb')) for n in ('map.json', 'cmp.json', 'tpc.pkl')]; \
tarfile.open('/app/src.tgz').extractall('/app/src', filter='data')" \
 && python -c "import json, marshal; \
[marshal.dump(json.load(open('/app/data/%s.json' % n)), open('/app/data/%s.marshal' % n, 'wb')) for n in ('map', 'cmp')]" \
 && python /app/selector.py --imports /app/data/imports.marshal \
 && rm /app/src.tgz /app/data/*.gz /app/data/*.json \
 && python -m compileall -q /app/src/sqlglot /app/selector.py /app/tpgate.py \
 && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
