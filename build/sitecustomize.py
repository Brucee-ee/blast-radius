import os
if os.environ.get("BR_ATTRS"):
    try:
        import brhooks
        brhooks.install_early()
    except Exception:
        pass
if os.environ.get("BR_PROFILE"):
    try:
        import brprofile
        brprofile.install()
    except Exception as e:
        import sys
        print("brprofile failed", e, file=sys.stderr)
if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage
        coverage.process_startup()
    except Exception:
        pass
