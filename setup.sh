#!/bin/sh
# Windows, macOS and Linux all use the same Python bootstrap and checks.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 bootstrap.py "$@"
