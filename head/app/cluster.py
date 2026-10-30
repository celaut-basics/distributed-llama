"""The workers and the llama-server of the head.

At the start of the instance the head asks the node (through the gateway) for N worker
instances, the child service `worker` that the packer put in the image. Each worker runs
llama.cpp's `rpc-server`. When the workers listen, the head starts `llama-server` with
`--rpc <worker>,<worker>,...`: llama.cpp splits the layers of the model across the
workers and sends them their tensors over the wire. The model file is only in the head.

Funding follows nodo #563: the build of a child comes out of the child's own initial_mu.
So a worker gets the build that the node quotes (GetServiceEstimatedCost) plus
WORKER_FUNDED_SECONDS of its runtime (5 minutes), and the head tops it up from its own
balance (ModifyDeposit) every 30 seconds while it runs. A stop of the head from outside
(`nodo kill`, a client's StopService) kills its VM at once: the head cannot stop its
workers then, and the node does not stop them either. A worker then lives until its
balance ends (at most WORKER_FUNDED_SECONDS), and the node marks that balance as lost
(`refund` with `{"lost": "father_gone"}`). The caller that stops the head should stop its
workers first (bench/run_case.sh does).

If a worker cannot start, or stops later, the head stops the other workers, starts
llama-server on its own CPUs (no `--rpc`) and tells the user in the next answer.
"""

import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request

from . import gateway

log = logging.getLogger("cluster")


def env_int(name, default):
    return int(os.environ.get(name) or default)


LLAMA_BIN = os.environ.get("LLAMA_BIN_DIR", "/opt/llama/bin")
MODEL = os.environ.get("LLAMA_MODEL", "/opt/model/model.gguf")
SERVICE_ROOT = os.environ.get("SERVICE_ROOT", "/service")  # where the packer puts children
SERVER_PORT = 8081  # llama-server, on 127.0.0.1 only
WORKER_PORT = 50052  # the slot of rpc-server in worker/<arch>/.service/service.json
INSUFFICIENT = re.compile(r"insufficient|not enough|does not cover|least initial_mu|balance",
                          re.IGNORECASE)


class Cluster:
    def __init__(self):
        self.want = env_int("LLAMA_WORKERS", 2)
        self.threads = env_int("LLAMA_THREADS", os.cpu_count() or 1)
        self.ctx = env_int("LLAMA_CTX", 4096)
        self.ngl = os.environ.get("LLAMA_NGL") or "999"  # layers on the workers: all
        # Short increments: the node does not stop the children of an instance that is
        # killed, and the balance that a child holds when its father is gone is lost. A
        # worker funded for 5 minutes at a time dies soon after its head.
        self.funded_s = env_int("WORKER_FUNDED_SECONDS", 300)
        self.topup_below_s = env_int("WORKER_TOPUP_BELOW_SECONDS", 150)
        self.topup_s = env_int("WORKER_TOPUP_SECONDS", 300)
        self.start_timeout_s = env_int("WORKER_START_TIMEOUT_S", 900)
        self.ready_timeout_s = env_int("WORKER_READY_TIMEOUT_S", 300)
        self.start_retries = env_int("WORKER_START_RETRIES", 3)
        self.load_timeout_s = env_int("LLAMA_LOAD_TIMEOUT_S", 900)
        self.server_port = SERVER_PORT
        self.mode = "starting"
        self.addresses = []
        self.workers = []  # dicts: n, token, address, initial_mu, topped_up_mu
        self.failure = None
        self.node_url = None
        self.rate = None  # MU per second of one worker, from the quote
        self.build_mu = None
        self.proc = None
        self.marks = {}
        self.load_net = (0, 0)  # bytes in and out while llama-server loaded the model
        self.restarts = 0
        self._notice = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._ended = threading.Event()
        self._wake = threading.Event()
        self._stopping = False
        self.t0 = time.monotonic()

    # What the chat asks.

    def start(self):
        threading.Thread(target=self._run, name="cluster", daemon=True).start()

    def wait_ready(self, cancelled):
        while not self._ready.wait(1):
            if self._ended.is_set() or cancelled():
                return False
        return True

    def take_notice(self):
        with self._lock:
            notice, self._notice = self._notice, None
            return notice

    def failure_hint(self):
        if self.mode == "distributed":
            return "The service checks its workers; send the message again."
        return "Send the message again."

    def check_after_failure(self):
        self._wake.set()

    def boot_metadata(self):
        meta = {"boot_" + k + "_ms": v for k, v in self.marks.items()}
        if self.build_mu is not None:
            meta["worker_build_mu"] = self.build_mu
        if self.rate is not None:
            meta["worker_rate_mu_s"] = round(self.rate, 1)
        meta["worker_initial_mu"] = sum(w["initial_mu"] for w in self.workers)
        meta["worker_topped_up_mu"] = sum(w["topped_up_mu"] for w in self.workers)
        meta["server_restarts"] = self.restarts
        meta["worker_start_failures"] = sum(w.get("start_failures", 0) for w in self.workers)
        meta["load_net_rx_bytes"], meta["load_net_tx_bytes"] = self.load_net
        return meta

    @staticmethod
    def net_bytes():
        """(rx, tx) bytes of the network interfaces of the instance, loopback excluded."""
        rx = tx = 0
        try:
            with open("/proc/net/dev") as fh:
                for line in fh.readlines()[2:]:
                    name, data = line.split(":", 1)
                    if name.strip() == "lo":
                        continue
                    fields = data.split()
                    rx += int(fields[0])
                    tx += int(fields[8])
        except OSError:
            pass
        return rx, tx

    def _mark(self, name):
        self.marks[name] = round((time.monotonic() - self.t0) * 1000)
        log.info("mark %s at %d ms", name, self.marks[name])

    # The start.

    def _run(self):
        try:
            addresses = []
            if self.want > 0:
                try:
                    addresses = self._start_workers()
                except Exception as e:
                    log.exception("no worker")
                    self._set_notice("No worker could start (%s). The model runs on the head "
                                     "only." % _short(e))
            if not self._serve(addresses):
                if addresses:
                    self._fall_back("llama-server did not load the model with the workers")
                else:
                    raise RuntimeError("llama-server did not load the model")
            if self._ended.is_set():
                return
            self._monitor()
        except Exception as e:
            log.exception("the cluster failed")
            self.failure = "the model could not start: %s" % _short(e)
            self._ended.set()

    def _set_notice(self, text):
        with self._lock:
            self._notice = "*(%s)*" % text

    def _start_workers(self):
        static = os.environ.get("LLAMA_STATIC_WORKERS")
        if static:
            # A test hook: rpc-servers that already run, with no gateway (bench/, tests/).
            self.workers = []
            self._mark("workers_ready")
            return [a.strip() for a in static.split(",") if a.strip()]
        self.node_url, _ = gateway.read_config()
        worker_id = _dependencies().get("WORKER")
        if not worker_id:
            raise RuntimeError("the image has no worker service (.dependencies)")
        packed = gateway.PackedService(worker_id, SERVICE_ROOT)
        self._mark("workers_requested")
        self._quote(packed)
        numbers = list(range(1, self.want + 1))
        started = []
        if self.build_mu:
            # The node builds the worker at the first start: start one, then the others
            # (they owe no build after it).
            first = self._start_one(packed, numbers.pop(0))
            if first:
                started.append(first)
                self._quote(packed)
        threads, results = [], {}
        for n in numbers:
            t = threading.Thread(target=lambda n=n: results.__setitem__(n, self._start_one(packed, n)))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        started += [results[n] for n in numbers if results.get(n)]
        self._mark("workers_started")
        ready = [w for w in started if self._wait_listening(w)]
        for w in started:
            if w not in ready:
                self._stop_worker(w)
        if not ready:
            raise RuntimeError("no worker answered on its port")
        self.workers = ready
        self._mark("workers_ready")
        if len(ready) < self.want:
            self._set_notice("%d of %d workers started." % (len(ready), self.want))
        return [w["address"] for w in ready]

    def _quote(self, packed):
        # A quote with initial_mu 1 MU: the cost is the build plus that 1 MU (nodo #563).
        cost = gateway.estimated_cost(self.node_url, packed, gateway.configuration(initial_mu=1))
        self.build_mu = max(0, gateway.mu(cost.cost) - 1)
        loop = cost.maintenance_seconds_loop or 1
        self.rate = max(1.0, gateway.mu(cost.init_maintenance_cost) / loop)
        log.info("worker quote: build %d MU, %.1f MU/s", self.build_mu, self.rate)

    def _start_one(self, packed, n):
        initial = self.build_mu + int(self.rate * self.funded_s * 1.5)
        deadline = time.monotonic() + self.start_timeout_s
        failures = 0
        while True:
            t = time.monotonic()
            try:
                instance = gateway.start_service(self.node_url, packed,
                                                 gateway.configuration(initial_mu=initial))
            except gateway.GatewayError as e:
                text = str(e)
                if INSUFFICIENT.search(text) and time.monotonic() < deadline:
                    log.warning("worker %d waits for funds: %s", n, text)
                    time.sleep(15)
                    continue
                # Starts at the same time can fail on the node (seen on nodo dev 97906c7c:
                # "Failed to apply default deny firewall policy for VM"): try again.
                failures += 1
                if failures <= self.start_retries and time.monotonic() < deadline:
                    log.warning("worker %d did not start (try %d): %s", n, failures, text)
                    time.sleep(2 * failures + n)
                    continue
                log.error("worker %d did not start: %s", n, text)
                return None
            address = gateway.slot_address(instance, WORKER_PORT)
            log.info("worker %d started in %.1f s at %s", n, time.monotonic() - t, address)
            w = {"n": n, "token": instance.token, "address": address, "initial_mu": initial,
                 "topped_up_mu": 0, "start_failures": failures}
            if not address:
                self._stop_worker(w)
                return None
            return w

    def _wait_listening(self, w):
        host, port = w["address"].rsplit(":", 1)
        deadline = time.monotonic() + self.ready_timeout_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, int(port)), timeout=3):
                    return True
            except OSError:
                time.sleep(1)
        log.error("worker %d does not listen at %s", w["n"], w["address"])
        return False

    def _serve(self, addresses):
        """Start llama-server (with the workers, if any) and wait until it loaded the model."""
        args = [os.path.join(LLAMA_BIN, "llama-server"), "-m", MODEL, "--host", "127.0.0.1",
                "--port", str(SERVER_PORT), "-t", str(self.threads), "-c", str(self.ctx),
                "-np", "1", "--no-webui"]
        if addresses:
            args += ["--rpc", ",".join(addresses), "-ngl", self.ngl]
        else:
            args += ["-ngl", "0"]
        args += (os.environ.get("LLAMA_SERVER_ARGS") or "").split()
        self.addresses = list(addresses)
        self.mode = ("distributed" if addresses else
                     "local" if self.want == 0 else "degraded")
        self._mark("server_spawned")
        log.info("llama-server: %s", " ".join(args))
        rx0, tx0 = self.net_bytes()
        logfile = open("/tmp/llama-server.log", "ab")
        self.proc = subprocess.Popen(args, stdout=logfile, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, LD_LIBRARY_PATH=LLAMA_BIN))
        deadline = time.monotonic() + self.load_timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                log.error("llama-server ended with %s: %s", self.proc.returncode, _tail())
                return False
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % SERVER_PORT,
                                            timeout=5) as r:
                    if r.status == 200:
                        # The tensors that went to the workers while the model loaded.
                        rx, tx = self.net_bytes()
                        self.load_net = (rx - rx0, tx - tx0)
                        self._mark("model_loaded")
                        self._ready.set()
                        return True
            except OSError:
                pass
            time.sleep(0.5)
        return False

    # While the instance runs.

    def _monitor(self):
        next_fund = 0
        while not self._stopping:
            self._wake.wait(10)
            self._wake.clear()
            if self._stopping:
                return
            if self.proc.poll() is not None:
                code = self.proc.returncode
                log.error("llama-server ended with %s: %s", code, _tail())
                if self.mode == "distributed":
                    gone = self._gone_worker()
                    self._fall_back("worker %d stopped" % gone["n"] if gone
                                    else "llama-server stopped (exit %s)" % code)
                else:
                    self.restarts += 1
                    self._ready.clear()
                    if self.restarts > 3 or not self._serve([]):
                        self.failure = "llama-server stopped and does not start again"
                        self._ended.set()
                        return
                continue
            if self.workers and time.monotonic() >= next_fund:
                next_fund = time.monotonic() + 30
                gone = self._fund()
                if gone:
                    self._fall_back("worker %d stopped" % gone["n"])

    def _gone_worker(self):
        for w in self.workers:
            try:
                gateway.metrics_balance(self.node_url, w["token"])
            except Exception:
                return w
        return None

    def _fund(self):
        """Top up each worker from the head. Return a worker that is gone, or None."""
        for w in self.workers:
            try:
                balance = gateway.metrics_balance(self.node_url, w["token"])
            except Exception as e:
                log.error("worker %d: %s", w["n"], e)
                return w
            if balance is not None and balance < self.rate * self.topup_below_s:
                add = int(self.rate * self.topup_s)
                try:
                    out = gateway.modify_deposit(self.node_url, w["token"], add)
                    if out is not None and out.success:
                        w["topped_up_mu"] += add
                        log.info("worker %d: balance %d MU, topped up %d MU", w["n"], balance, add)
                    else:
                        log.warning("worker %d: top-up refused: %s", w["n"], out)
                except Exception as e:
                    log.warning("worker %d: top-up failed: %s", w["n"], e)
        return None

    def _fall_back(self, reason):
        """Stop the workers and serve from the head alone."""
        log.warning("fall back to the head alone: %s", reason)
        self._ready.clear()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for w in self.workers:
            self._stop_worker(w)
        self.workers = []
        self._set_notice("%s. The model now runs on the head only, without workers" % reason)
        self._mark("fallback")
        if not self._serve([]):
            self.failure = "the model could not start on the head alone"
            self._ended.set()

    def _stop_worker(self, w):
        try:
            refund = gateway.stop_service(self.node_url, w["token"])
            log.info("worker %d stopped, refund %d MU", w["n"], refund)
        except Exception as e:
            log.warning("worker %d: stop failed: %s", w["n"], e)

    def stop(self):
        """At the end of the instance: stop llama-server and the workers."""
        self._stopping = True
        self._wake.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        for w in self.workers:
            self._stop_worker(w)


def _dependencies():
    out = {}
    try:
        with open(os.path.join(SERVICE_ROOT, ".dependencies")) as fh:
            for line in fh:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    out[key] = value
    except OSError:
        pass
    return out


def _short(error):
    return str(error)[:300]


def _tail(path="/tmp/llama-server.log", n=1500):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - n))
            return fh.read().decode("utf-8", "replace").replace("\n", " | ")
    except OSError:
        return ""
