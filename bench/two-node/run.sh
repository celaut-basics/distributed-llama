#!/bin/bash
# run.sh <step> : the two-node case of the benchmark. Node A (container dla) runs the head;
# its workers do not fit on A, so A gives them to node B (container dlb). Adapted from
# celaut-basics/demo-service tools/two-node (ct.sh, in-install.sh). No ledger: B credits
# A's client with free MU (nodo credit_client), the wallets of both nodes are new and empty.
#
#   up                 two containers, nodo dev in each (~15 min)
#   configure          A: room for the head only; B: a third of A's prices; A -> B; credit
#   import <bee>       import the head export (it carries the worker) on A and B
#   prebuild <head> <worker>   B builds the worker, A builds the head (one run each, stopped)
#   case <label> <head> <workers>   one run (../run_case.sh on node A)
#   down               stop the instances and remove both containers (ntbase stays)
set -u
D=$(cd "$(dirname "$0")" && pwd)
A=dla; B=dlb
in_ct() { local ct=$1; shift; systemd-run -M "$ct" --wait -q -P -p TimeoutStartSec=3600 bash -c "$*"; }

case "${1:?step}" in
up)
  bash "$D/ct.sh" base
  CT=$A NETN=141 bash "$D/ct.sh" up
  CT=$B NETN=142 bash "$D/ct.sh" up
  for ct in $A $B; do
    cp "$D/in-install.sh" "/var/lib/machines/$ct/root/"
    systemd-run -M $ct --unit=dl-install --collect -p TimeoutStartSec=3600 bash /root/in-install.sh
  done
  timeout 3000 bash -c "until grep -q INSTALL_DONE /var/lib/machines/$A/root/install.log \
    && grep -q INSTALL_DONE /var/lib/machines/$B/root/install.log; do sleep 15; done"
  grep -h INSTALL_RC /var/lib/machines/$A/root/install.log /var/lib/machines/$B/root/install.log
  ;;
configure)
  # A: room for the head (3 GiB, 4 vCPU) and not for a worker (2 GiB, 4 vCPU) too.
  TOTAL=$(python3 -c 'import os; print(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))')
  RAM=$(python3 -c "print(round(3.3e9 / $TOTAL, 5))")
  CPU=$(python3 -c "import os; print(round(4.5 / os.cpu_count(), 4))")
  in_ct $A "nodo config set host_limits.ENABLED=true && nodo config set host_limits.MAX_CPU_SHARE=$CPU && nodo config set host_limits.MAX_RAM_SHARE=$RAM"
  # B: a third of A's prices, so that A's own price covers B (delegated_price.covers).
  in_ct $B "nodo config set pricing.CPU_MU_PER_VCPU_HOUR=1333333 && nodo config set pricing.RAM_MU_PER_GIB_HOUR=333333 && nodo config set pricing.DISK_MU_PER_GIB_HOUR=33333"
  sleep 20
  for ct in $A $B; do in_ct $ct "nodo firewall-compat apply >/dev/null 2>&1; nodo doctor | tail -1"; done
  BADDR=$(in_ct $B "nodo status" | sed -n 's/^Address: *\([0-9.]*:[0-9]*\).*/\1/p')
  in_ct $A "nodo connect $BADDR"
  CLIENT=$(in_ct $A "nodo peers" | sed -n 's/^ *Remote Client ID: *//p' | head -1)
  in_ct $B "nodo credit_client $CLIENT 2000000000"
  ;;
import)
  BEE=${2:?head export .bee}
  mkdir -p /var/tmp/dl-bee && cp "$BEE" /var/tmp/dl-bee/head.bee
  # /var/tmp/dl-bee is bound read-only at /var/tmp/bee in each container (ct.sh).
  for ct in $A $B; do in_ct $ct "nodo import /var/tmp/bee/head.bee 2>&1 | tail -2"; done
  ;;
prebuild)
  HEAD=${2:?head id}; WORKER=${3:?worker id}
  in_ct $B "timeout 900 nodo execute --name build-worker --no-input $WORKER < /dev/null > /root/build-worker.log 2>&1; sleep 5; nodo kill build-worker; nodo instances | tail -2"
  in_ct $A "timeout 900 nodo execute --name build-head --no-input -e LLAMA_WORKERS 0 $HEAD < /dev/null > /root/build-head.log 2>&1; sleep 5; nodo kill build-head; nodo instances | tail -2"
  ;;
case)
  OUT=${OUT:-$D/../results/raw} bash "$D/../run_case.sh" "$2" "$3" "$4" $A
  ;;
down)
  for ct in $A $B; do
    in_ct $ct "for i in \$(nodo instances --json | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get(\"instances\",[]); print(\" \".join(i[\"id\"] for i in d))'); do nodo kill \$i; done" 2>/dev/null
  done
  CT=$A NETN=141 bash "$D/ct.sh" down
  CT=$B NETN=142 bash "$D/ct.sh" down
  ;;
*)
  echo "unknown step: $1"; exit 2 ;;
esac
