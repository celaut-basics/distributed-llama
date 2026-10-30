# Benchmark: how Celaut handles a distributed workload

The question is not "is distributed llama.cpp fast" (on one machine it is not), but how
Celaut's scheduling, networking and billing behave when a service splits its work across
child instances: on one node, and across two nodes with delegation.

All numbers: 2026-10-30, medians of 3 runs. Raw per-run data: [`bench/results/runs.csv`](bench/results/runs.csv),
[`bench/results/summary.json`](bench/results/summary.json).

## Setup

| | |
|---|---|
| Host | Alienware, AMD Ryzen 5 5600X (6 cores / 12 threads), Windows 11, WSL2 Ubuntu 22.04 (kernel 6.6.87.2), 12 vCPU, 23.4 GiB RAM. The RTX 3090 is not used |
| Node (one node) | the live node of the host, nodo dev `97906c7c`, Cloud Hypervisor v51.1. Used as a client only (no config change), free local credit |
| Nodes (two nodes) | two fresh nodo dev `ecf07b59` installs in `systemd-nspawn` containers on the same host (`bench/two-node/`): A runs the head, B a third of A's prices; A has room for the head only (`host_limits` 4.5 GB RAM, CPU share 0.75 = 4.5 vCPU), so its workers go to B. B credits A's client with free MU; the wallets are empty |
| Service | head `bffa090f…` / worker `6f7dca75…` (llama.cpp v0.8.0, CPU, all x86-64 variants). Head 4 vCPU, 3 GiB; worker 4 vCPU, 2 GiB |
| Model | Qwen2.5-1.5B-Instruct Q4_K_M (1.1 GB), context 4096 |
| Workload | one fresh instance per run; one message: [`bench/prompt.txt`](bench/prompt.txt) (359 prompt tokens with the chat template), exactly 128 output tokens (`LLAMA_IGNORE_EOS`), temperature 0, seed 42, through the chat interface (`bench/chatbench.py`, `Connect` stream) |
| Threads | 4 everywhere (`llama-bench -t 4`, head `-t 4`, each `ggml-rpc-server -t 4` = one per vCPU) |

Cases:

- `host-w0`: the head code on the host, no Celaut, llama-server alone (`bench/host_baseline.sh`).
- `host-wN`: the same with N `ggml-rpc-server` processes on 127.0.0.1: RPC without Celaut.
- `live-wN`: Celaut, one node: the head with N workers (`bench/live_cases.sh`).
- `two-wN`: Celaut, two nodes: head on A, the N workers delegated to B (`bench/two-node/run.sh`).
- Plain llama.cpp (`llama-bench -t 4 -p 359 -n 128 -r 3`): **pp 162.9 tok/s, tg 33.1 tok/s**.

## Results

| case | pp tok/s | tg tok/s | TTFT s | cold start s | tensors sent at load | RPC per answer (in / out) | CPU cores busy (gen) | MU per run: runtime + build |
|---|---|---|---|---|---|---|---|---|
| llama-bench (plain llama.cpp) | 162.9 | 33.1 | - | - | - | - | - | - |
| host-w0 (no Celaut) | 162.3 | 30.8 | 2.21 | 1.6 | - | - | 3.7 | - |
| host-w1 | 177.4 | 29.3 | 2.03 | 2.6 | (loopback) | (loopback) | 3.6 | - |
| host-w2 | 175.1 | 28.2 | 2.05 | 2.6 | (loopback) | (loopback) | 3.7 | - |
| host-w3 | 174.0 | 26.9 | 2.07 | 2.6 | (loopback) | (loopback) | 4.1 | - |
| **live-w0** (Celaut, head alone) | 157.6 | 29.7 | 2.29 | 32.2 | 0 | 0 | 3.9 | 0.22 M + 0 |
| **live-w1** | 163.2 | 27.9 | 2.21 | 40.6 | 982 MB | 78 / 11 MB | 3.6 | 0.52 M + 0 |
| **live-w2** | 154.1 | 26.1 | 2.34 | 52.4 | 982 MB | 81 / 15 MB | 4.1 | 0.84 M + 0 |
| **live-w3** | 151.6 | 24.9 | 2.38 | 54.2 | 982 MB | 84 / 19 MB | 4.2 | 1.17 M + 0 |
| **two-w1** (workers on peer B) | 171.0 | 28.4 | 2.10 | 63.8 | 982 MB | 78 / 11 MB | 3.5 | 0.64 M + 10 M |
| **two-w2** | 167.6 | 26.8 | 2.15 | 59.7 | 982 MB | 81 / 15 MB | 4.0 | 0.91 M + 20 M |
| **two-w3** | 162.7 | 25.6 | 2.21 | 84.9 | 982 MB | 84 / 19 MB | 4.4 | 1.65 M + 30 M |

- **pp / tg**: llama-server's own timings of the answer (prompt processing, generation).
- **TTFT**: client side, from the moment the answer goes `IN_PROGRESS` (model ready) to the
  first text delta. It is prompt processing plus the first token.
- **Cold start**: from the start command (`nodo execute`, or the process start on the host)
  to the answer going `IN_PROGRESS`: VM start, worker starts, model load with the transfer
  of the tensors. The head and the worker were already built on the node (the first start
  of a new pack adds a build: 180 s for the head, about 40–50 s for the worker, 10 M MU each).
- **RPC**: bytes on the network interfaces of the head (`/proc/net/dev` in the guest).
- **CPU cores busy**: the sum, over the Cloud Hypervisor processes of the run (or the
  llama.cpp processes on the host), of the CPU time during generation divided by its
  duration. Per instance in a 3-worker run: 1.4 + 1.3 + 1.3 cores for the workers, 0.2 for
  the head.
- **MU**: from the node's ledger (`bench/ledger.py`): maintenance, build and tunnel charges
  of the head and its workers. Nothing was lost (`lost_mu` 0 in every run).

All 33 runs completed. With greedy decoding the text is the same in every run of a case,
and the **same in all six distributed Celaut cases** (1–3 workers, one or two nodes): the
split does not change the result.

### Cold start, in parts (medians, s)

| case | `nodo execute` | workers started and listening | model load (tensors to workers) |
|---|---|---|---|
| live-w0 | 27.3 | - | 7.0 (local mmap) |
| live-w1 | 24.0 | 8.1 | 9.5 |
| live-w2 | 24.0 | 19.1 | 10.0 |
| live-w3 | 24.1 | 23.4 | 8.0 |
| two-w1 | 35.9 | 20.2 | 6.5 |
| two-w2 | 25.2 | 29.2 | 6.0 |
| two-w3 | 25.3 | 53.9 | 6.0 |

## What the numbers say

**Speed.** As expected, splitting a small model over CPU workers does not make one
conversation faster. llama.cpp RPC is a pipeline: for each token the layers run on one worker
after the other, so only one worker computes at a time. Each worker adds one network round
trip per token: generation goes 29.7 → 27.9 → 26.1 → 24.9 tok/s for 0 → 3 workers on one
node, about 2 ms per token per hop. The same slope exists without Celaut (host loopback:
30.8 → 26.9), so the cost is llama.cpp RPC, not Celaut's network. Celaut's own overhead for
the same work is small: 3–4 % for the head alone (live-w0 vs host-w0; the VM vs the host),
and virtio networking between instances (or to a peer through the veth of the containers)
costs about the same as loopback. Prompt processing is a little faster with one worker (a
fresh 4-vCPU VM; the head thread does nothing), and drops with more hops.

**Scheduling.** The node places and starts the workers as the head asks, and delegation works
as designed: on node A the head stays local and each worker goes to peer B (cheaper, and B
had built it), and the head reaches B's published port with no change to its code. Two
things cost time:

- `nodo execute` of a built 1.4 GB service takes ~24 s (the 3.7 GB root disk of the instance
  is made for each start). That is most of the cold start of the head.
- Concurrent `StartService` calls from one parent fail on nodo dev `97906c7c`: "Failed to
  apply default deny firewall policy for VM …" for 1–2 of 3 parallel starts (in the first
  set of runs, 5 of 6 multi-worker runs got fewer workers than asked). The head now retries a
  failed start (`WORKER_START_RETRIES`); with that, every run got all its workers. A
  worker start that waits for a retry is why live-w2/w3 take 19–23 s instead of 8 s.

**Resources and billing.** The node bills what the instances reserve, not what they use:
4 vCPU per worker, but the pipeline keeps about 4 cores busy in total whatever the number of
workers (3 workers: 1.3 cores each). So a run with 3 workers costs 5× the MU of the head
alone (1.17 M vs 0.22 M MU) for a slower answer. The ledger is exact and easy to follow:
`child_start` from the head to each worker, the worker's `build` and `maintenance` out of its
own balance (nodo #563), `refund` back to the head when a worker stops first. Two billing
findings:

- **A node charges its own build for a delegated child.** In the two-node runs A charges its
  client `BUILD_MU` (10 M MU) for every worker it gives to B, at every start, although B has
  the build and charges A no build (B's ledger: about 0.7 M MU per worker, all maintenance).
  So 94–95 % of the two-node cost is a build that nobody did.
- **Children are not stopped with their parent.** `nodo kill` of the head (or a client's
  `StopService`) ends the head's VM at once; its workers keep running, and when they end the
  node books their balance as lost (`refund` `{"lost": "father_gone"}`). The head therefore
  funds its workers 5 minutes at a time, and the benchmark stops the workers first.
- Prices follow host load: the quoted worker rate went from 5.8 k to 15 k MU/s while a pack
  ran on the host (the scarcity multiplier), so a parent must size child budgets from a fresh
  quote, as the head does.

**Network.** Loading sends 982 MB of tensors from the head to the workers (all layers, the
output layer included); with one worker that is the whole model, with three it is split.
Each answer moves about 600 KB per generated token back to the head: llama.cpp v0.8.0
offloads the output layer first, so the logits of the whole vocabulary (151 936 floats) come
back over RPC each token. That is fine on a host or a LAN (78 MB per 128 tokens), but it is
the first thing to fix for workers on remote peers.

**Bottom line.** Celaut ran a real distributed workload end to end: a parent that starts,
funds, watches, tops up and stops its own children, inter-instance traffic of about 1 GB,
delegation to a peer, and a ledger that accounts for every MU. The overhead that Celaut adds
on top of llama.cpp RPC is small at run time (a few %), large at start time (instance disk
creation, serialized/failed concurrent starts), and the two-node billing has a real bug
(the phantom build). The speed limits are llama.cpp RPC's, and they are expected for CPU
workers on one host; the case for this design is models that do not fit one node, and GPU
workers.

## Reproduce

```sh
# host baseline (non-root user, llama.cpp built with the same flags, model in /srv/dl/models)
bench/host_baseline.sh
# one node (root for the nodo CLI; the client runs as nrtest)
bench/live_cases.sh <head id>
# two nodes
bench/two-node/run.sh up && bench/two-node/run.sh configure && bench/two-node/run.sh client
bench/two-node/run.sh import <head export .bee>
#   B builds the worker (run the head there once, or the worker), A builds the head with
#   network.DELEGATE_EXECUTION=false for that one start, B must not hold a build of the head
bench/two-node/run.sh case two-w2-r1 <head id> 2
bench/summarize.py bench/results/raw
```
