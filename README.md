# Blast Radius selector

Picks, for each sqlglot patch, the tests that could fail, then splits them over the cheapest number
of workers (minimising busiest-worker seconds + 2 s per worker).

## How it decides
Maps were built once from the pinned sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`
(`data/map.json`, built by `build/`): per-test line coverage, which sqlglot classes each test
instantiates, and which fixture files each test reads. At run time `selector.py` parses each patch and:

* sqlglot `.py` changes: parses the old and patched source. If only comments/blank lines/docstrings
  changed, nothing runs. Otherwise each changed statement is mapped to the tests that executed it;
  signature changes map to the whole function; additions map to the enclosing function; module and
  class-body (import-time) changes map to the tests that instantiated the class, or that use the
  changed names.
* fixture files: the tests that read them. Test files: the changed test functions, or the whole file.
* anything it cannot place (syntax errors, renames, config files, unknown shapes) runs every test.

No table from patches or kinds of edit to failing tests exists; the maps come only from the pinned commit.

## Run
`make sample` (Docker) builds the image and scores the 30 sample commits. Locally without Docker:
`BR_DATA=<dir with commits/ and tests.json> python selector.py`.

## Rebuilding the map
`build/covplugin.py` is a pytest plugin that records per-test coverage contexts, including process-pool
workers and fixture opens. See `build/build_map.py` for the exact steps (coverage 7.17.4, the kit's
`requirements.lock`, `SKIP_INTEGRATION=0`).
