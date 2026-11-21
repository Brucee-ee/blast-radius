# Blast Radius selector

Picks, for each sqlglot patch, the tests that could fail, then splits them over the cheapest number
of workers (minimising busiest-worker seconds + 2 s per worker).

## How it decides
Maps were built once from the pinned sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`
(`data/map.json`, built by `build/`): per-test line coverage, which sqlglot classes each test
instantiates, which fixture files each test reads, and which class attributes each test reads through
which class (recorded with a descriptor installed on every sqlglot class as it is created). At run time
`selector.py` parses each patch and:

* sqlglot `.py` changes: parses the old and patched source. If only comments/blank lines/docstrings
  changed, nothing runs. Otherwise each changed statement is mapped to the tests that executed it;
  signature changes map to the whole function; additions map to the enclosing function; module and
  class-body (import-time) changes map to the tests that instantiated the class, or that use the
  changed names.
* a changed plain class attribute (e.g. a dialect flag or a parser table) maps to the tests that read
  that attribute through that class or a subclass. When the value is also used while classes are
  created or modules imported (metaclass, `__init_subclass__`, module-level code), or is copied into
  something else at import time, it falls back to the wider class-level selection.
* an edit inside one statement maps to the tests that executed that statement; an edit that only
  changes comments, layout or a docstring selects nothing. An edit confined to one comparison maps
  to the tests that evaluated it, minus those for which a comparison profile shows the result cannot
  change (`is` and `==` always agreed, or the two sides of `<`/`<=` were never equal).
* edits to shared test code (e.g. the `Validator` base class or `tests/helpers.py`) select the tests
  of every test file importing it.
* fixture files: the tests that read them. Test files: the changed test functions, or the whole file.
* anything it cannot place (syntax errors, renames, config files, unknown shapes) runs every test.

No table from patches or kinds of edit to failing tests exists; the maps come only from the pinned commit.

## Run
`make sample` (Docker) builds the image and scores the 30 sample commits. Locally without Docker:
`BR_DATA=<dir with commits/ and tests.json> python selector.py`.

## Rebuilding the map
`build/run_build.sh <sqlglot checkout at eb11aec> <tests.json>` regenerates `data/map.json` (two
full test runs: one under coverage with the recording hooks, one with comparisons instrumented by
`build/brprofile.py`).
`build/covplugin.py` (pytest plugin) records per-test coverage contexts, including process-pool workers
and class/module setup, fixture opens and class usage; `build/brhooks.py` (loaded by
`build/sitecustomize.py` before sqlglot is imported) records class-attribute reads. Needs coverage 7.17.4
and the kit's `requirements.lock`; tests run with `SKIP_INTEGRATION=0`.
