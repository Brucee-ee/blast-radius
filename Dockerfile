# Same base image the labels were made on (its Python and SQLite decide which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector uses only the standard library. data/ holds maps built from the pinned sqlglot
# commit (see build/); src/ is a copy of that commit's sqlglot/ and tests/ python files, read
# by the selector for AST analysis. Nothing is downloaded at build time.
COPY selector.py /app/selector.py
COPY data /app/data
COPY src /app/src
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
