#!/bin/bash
# Run unittest under the tool interpreter and pytest-style regressions under the
# available development interpreter. Any first-phase failure is terminal.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON_FOR_TESTS:-$HOME/.local/share/uv/tools/longrun/bin/python}"
export PYTHON_FOR_TESTS="$PY"
cd tests
if [ "$#" -gt 0 ]; then
  exec "$PY" -m unittest -v "$@" 2>&1
fi
"$PY" -m unittest -v 2>&1
cd ..
PYTHONPATH=. python3 -m pytest -q
