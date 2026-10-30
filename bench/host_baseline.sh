#!/bin/bash
# host_baseline.sh : the cases with no Celaut, on this host, as a user that is not root.
#   llama-bench  : plain llama.cpp, 4 threads, the prompt length of the chat cases, 128 tokens
#   host-w0      : the head (same code, same model) with no worker, 4 threads
#   host-wN      : the head with N ggml-rpc-servers on 127.0.0.1 (4 threads each)
# Each head case runs 3 times, each run a new process (cold start = process start to ready).
set -u
D=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$D/results/raw}; mkdir -p "$OUT"
PY=${PY:-/srv/dl/venv/bin/python}
B=${LLAMA_BIN_DIR:-/srv/dl/llama.cpp/build/bin}
M=${LLAMA_MODEL:-/srv/dl/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}
export LD_LIBRARY_PATH=$B
PROMPT_TOKENS=${PROMPT_TOKENS:-330}
"$B/llama-bench" -m "$M" -t 4 -p "$PROMPT_TOKENS" -n 128 -r 3 -o json > "$OUT/llama-bench.json" 2> "$OUT/llama-bench.err"
for W in 0 1 2 3; do
  for R in 1 2 3; do
    L=host-w$W-r$R; PIDS=""; ADDRS=""
    $PY "$D/cpusample.py" "$OUT/$L.cpu.jsonl" & S=$!
    T0=$(date +%s.%N)
    for i in $(seq 1 $W); do
      "$B/ggml-rpc-server" -H 127.0.0.1 -p $((50070 + i)) -t 4 > /tmp/dl-hw$i.log 2>&1 & PIDS="$PIDS $!"
      ADDRS="$ADDRS${ADDRS:+,}127.0.0.1:$((50070 + i))"
    done
    (cd "$D/../head" && LLAMA_BIN_DIR=$B LLAMA_MODEL=$M LLAMA_WORKERS=$W LLAMA_STATIC_WORKERS=$ADDRS \
      LLAMA_THREADS=4 LLAMA_TEMPERATURE=0 LLAMA_SEED=42 LLAMA_IGNORE_EOS=1 LLAMA_MAX_TOKENS=128 \
      PORT=18090 exec $PY -m app.main > "$OUT/$L.head.log" 2>&1) & H=$!
    $PY "$D/chatbench.py" 127.0.0.1:18090 --t0 "$T0" --timeout 600 > "$OUT/$L.json" 2> "$OUT/$L.err"
    sleep 2; kill $S
    pkill -P $H 2>/dev/null; kill $H $PIDS 2>/dev/null; wait $H $PIDS 2>/dev/null
    sleep 2
    tail -c 200 "$OUT/$L.json"; echo
  done
done
