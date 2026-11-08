#!/bin/sh
# tools/bento sim <board> [arguments]: builds the simulator if it needs it, and opens that board's window.
# (Make would take --demo and the like as options of its own, so tools/bento comes here rather than to `make run`.)
set -e
here=$(cd "$(dirname "$0")" && pwd)
board=$1
shift
make -s -C "$here" sharp
exec "$here/build/bento-sim-sharp" --board "$board" "$@"
