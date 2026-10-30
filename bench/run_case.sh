#!/bin/bash
# run_case.sh <label> <head service id> <workers> [<node>]
#
# One benchmark run of the head on a node: execute it with <workers> workers, top it up
# from the node's own (local, free) credit, send the fixed prompt through the chat
# interface (bench/chatbench.py, as the non-root user nrtest), sample the CPU of every
# instance, stop the workers then the head, and write results/<label>.json.
#
# <node> is "live" (the node of this host, /nodo) or the name of an nspawn container
# (bench/two-node/). Root only for the nodo CLI; the client runs as nrtest.
set -u
LABEL=$1; SID=$2; W=$3; NODE=${4:-live}
D=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$D/results/raw}; mkdir -p "$OUT"
PY=${PY:-/srv/dl/venv/bin/python}
TOPUP=${TOPUP:-200000000}
if [ "$NODE" = live ]; then n() { nodo "$@"; }; else n() { systemd-run -M "$NODE" --wait -q -P nodo "$@"; }; fi

inst_json() { n instances --json 2>/dev/null; }
field() { python3 -c "
import json, re, sys
d = json.load(sys.stdin)
d = d if isinstance(d, list) else d.get('instances', [])
for i in d:
    if i.get('name') == '$1':
        if '$2' == 'ip':
            m = re.search(r'(\d+\.\d+\.\d+\.\d+):(\d+)', json.dumps(i.get('ip') or i))
            print(m.group(1) + ':' + m.group(2) if m else '')
        else:
            print(i.get('$2', ''))
        break"; }

$PY "$D/cpusample.py" "$OUT/$LABEL.cpu.jsonl" & SAMPLER=$!
BEFORE=$(inst_json | python3 -c "import json,sys; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get('instances',[]); print(' '.join(i['id'] for i in d))")
T0=$(date +%s.%N)
n execute --name "$LABEL" --no-input \
    -e LLAMA_WORKERS "$W" -e LLAMA_IGNORE_EOS 1 -e LLAMA_MAX_TOKENS 128 \
    -e LLAMA_TEMPERATURE 0 -e LLAMA_SEED 42 "$SID" < /dev/null > "$OUT/$LABEL.exec.log" 2>&1
T_EXEC=$(date +%s.%N)
TOKEN=$(inst_json | field "$LABEL" id)
[ -n "$TOKEN" ] && n increase_deposit "$TOKEN" "$TOPUP" > "$OUT/$LABEL.topup.log" 2>&1
ADDR=""
for i in $(seq 1 120); do ADDR=$(inst_json | field "$LABEL" ip); [ -n "$ADDR" ] && break; sleep 1; done
# The head's slot 8080: on the live node the guest address; on a container node, the
# published address of the node (the host reaches it through the container's veth).
echo "token=$TOKEN addr=$ADDR exec_s=$(python3 -c "print(round($T_EXEC-$T0,3))")" > "$OUT/$LABEL.info"
su nrtest -s /bin/bash -c "$PY $D/chatbench.py $ADDR --t0 $T0 --timeout 1800" > "$OUT/$LABEL.json" 2> "$OUT/$LABEL.err"
sleep 2
kill $SAMPLER
# The instances of this run: the head and its workers (local ones; delegated workers run
# on the peer and are listed there).
AFTER=$(inst_json | python3 -c "import json,sys; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get('instances',[]); print(json.dumps(d))")
echo "$AFTER" > "$OUT/$LABEL.instances.json"
BAL=$(inst_json | field "$LABEL" balance)
for id in $(echo "$AFTER" | python3 -c "import json,sys; print(' '.join(i['id'] for i in json.load(sys.stdin)))"); do
  case " $BEFORE " in *" $id "*) continue;; esac
  [ "$id" = "$TOKEN" ] && continue
  n kill "$id" --json >> "$OUT/$LABEL.kill-workers.log" 2>&1
done
n kill "$TOKEN" --json > "$OUT/$LABEL.kill-head.log" 2>&1
echo "balance_before_kill=$BAL" >> "$OUT/$LABEL.info"
sleep 3
DB=/nodo/storage/database.sqlite; [ "$NODE" = live ] || DB=/var/lib/machines/$NODE/nodo/storage/database.sqlite
python3 "$D/ledger.py" "$DB" "$TOKEN" > "$OUT/$LABEL.ledger.json" 2>&1
tail -c 300 "$OUT/$LABEL.json"; echo
