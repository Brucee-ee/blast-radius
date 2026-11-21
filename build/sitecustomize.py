import os
if os.environ.get("BR_ATTRS"):
    try:
        import brhooks
        brhooks.install_early()
    except Exception:
        pass
if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage
        coverage.process_startup()
    except Exception:
        pass
