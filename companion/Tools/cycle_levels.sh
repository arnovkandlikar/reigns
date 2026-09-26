#!/bin/sh
# Debug builds only: walk the pet through every level once, 5 s apart, ending back at Calm.
# Assumes the pet starts at level 0 (0→1→2→3→4→0 = 5 steps).
DELAY="${1:-5}"
cd "$(dirname "$0")"
for step in 1 2 3 4 5; do
    sh next_level.sh
    if [ "$step" -lt 5 ]; then sleep "$DELAY"; fi
done
