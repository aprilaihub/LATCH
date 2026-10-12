#!/usr/bin/env python3
"""WP-012 entry point for freeze, execution, stop, and aggregation.

The implementation remains shared with the already audited E1-v2 runner; this
entry point exists so WP-012 command lines and process ownership are explicit.
"""

from run_wp007 import main


if __name__ == "__main__":
    raise SystemExit(main())
