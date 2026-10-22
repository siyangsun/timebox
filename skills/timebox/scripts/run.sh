#!/bin/sh
# Launcher: finds a working Python (python3, python, or the Windows py launcher) and runs timebox.py.
d=$(dirname "$0")
for py in python3 python py; do
  "$py" -c "" >/dev/null 2>&1 && exec "$py" "$d/timebox.py" "$@"
done
echo "timebox: no working python3/python/py on PATH" >&2
exit 0
