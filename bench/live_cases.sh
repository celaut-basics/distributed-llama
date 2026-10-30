#!/bin/bash
# live_cases.sh <head id> : the single-node cases on the node of this host, 3 runs each.
D=$(cd "$(dirname "$0")" && pwd)
for W in 0 1 2 3; do for R in 1 2 3; do bash "$D/run_case.sh" "live-w$W-r$R" "$1" "$W" live; done; done
