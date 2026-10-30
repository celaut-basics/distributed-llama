"""The model: a stream of tokens from the local llama-server, for the answers of `chat.Chat`.

llama-server listens on 127.0.0.1 only (inside the head). The head sends it the turns of
the conversation through the OpenAI-compatible `/v1/chat/completions` with `stream`, and
puts each piece of text in the answer as a `MessageDelta`. The chat template is the one in
the GGUF file.
"""

import http.client
import json
import os
import time
from types import SimpleNamespace


def env_int(name, default):
    return int(os.environ.get(name) or default)


def env_float(name, default):
    return float(os.environ.get(name) or default)


SYSTEM_PROMPT = os.environ.get("LLAMA_SYSTEM_PROMPT") or (
    "You are a helpful assistant. You run as a Celaut service: a small open model "
    "(Qwen2.5-1.5B-Instruct) on CPUs, with its layers split across worker instances.")
MAX_TOKENS = env_int("LLAMA_MAX_TOKENS", 512)
TEMPERATURE = env_float("LLAMA_TEMPERATURE", 0.7)
SEED = env_int("LLAMA_SEED", 42)
# For a benchmark: the answer has exactly LLAMA_MAX_TOKENS tokens.
IGNORE_EOS = (os.environ.get("LLAMA_IGNORE_EOS") or "").lower() in ("1", "true", "yes")
# The prompt is cut to this many characters of history (about 3 characters a token), so
# that the prompt and the answer stay in the context of the model.
CTX = env_int("LLAMA_CTX", 4096)
HISTORY_CHARS = max(2000, (CTX - MAX_TOKENS) * 3)


def build_messages(turns):
    """The messages of the request: the system prompt, then the last turns that fit."""
    kept, size = [], 0
    for role, text in reversed(turns):
        if kept and size + len(text) > HISTORY_CHARS:
            break
        kept.append({"role": role, "content": text})
        size += len(text)
    return [{"role": "system", "content": SYSTEM_PROMPT}] + list(reversed(kept))


class Backend:
    """What `chat.Chat` needs from the model, on top of a `cluster.Cluster`."""

    def __init__(self, cluster):
        self.cluster = cluster

    def wait_ready(self, cancelled):
        return self.cluster.wait_ready(cancelled)

    def not_ready_reason(self):
        return self.cluster.failure or "the model did not start"

    def take_notice(self):
        return self.cluster.take_notice()

    def generate(self, turns, on_text, on_reasoning, cancelled):
        """Stream one answer. Return SimpleNamespace(error, metadata)."""
        meta = {"mode": self.cluster.mode, "workers": len(self.cluster.addresses)}
        meta.update(self.cluster.boot_metadata())
        rx0, tx0 = self.cluster.net_bytes()
        t0 = time.monotonic()
        first = None
        timings = {}
        error = None
        done = False
        body = {"messages": build_messages(turns), "stream": True, "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE, "seed": SEED, "cache_prompt": True}
        if IGNORE_EOS:
            body["ignore_eos"] = True
        conn = http.client.HTTPConnection("127.0.0.1", self.cluster.server_port, timeout=900)
        try:
            conn.request("POST", "/v1/chat/completions", json.dumps(body),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(2000).decode("utf-8", "replace")
                try:
                    detail = json.loads(detail)["error"]["message"]
                except Exception:
                    pass
                return SimpleNamespace(error="the model refused the request: %s" % detail,
                                       metadata=meta)
            for raw in resp:
                if cancelled():
                    meta["cancelled"] = "true"
                    break
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                timings = chunk.get("timings") or timings
                for choice in chunk.get("choices") or ():
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        on_reasoning(delta["reasoning_content"])
                    if delta.get("content"):
                        if first is None:
                            first = time.monotonic()
                        on_text(delta["content"])
            if not done and not meta.get("cancelled"):
                raise ConnectionError("the stream ended before its end")
        except (OSError, http.client.HTTPException, ValueError) as e:
            # A worker that stops makes llama-server stop: the cluster falls back to the
            # head alone, and the next message works again.
            self.cluster.check_after_failure()
            error = ("the model stopped during the answer (%s). %s"
                     % (type(e).__name__, self.cluster.failure_hint()))
        finally:
            conn.close()
        rx1, tx1 = self.cluster.net_bytes()
        meta["answer_ms"] = round((time.monotonic() - t0) * 1000)
        if first is not None:
            meta["ttft_ms"] = round((first - t0) * 1000)
        meta["net_rx_bytes"] = rx1 - rx0
        meta["net_tx_bytes"] = tx1 - tx0
        for key, name in (("prompt_n", "prompt_tokens"), ("prompt_ms", "prompt_ms"),
                          ("prompt_per_second", "prompt_tps"), ("predicted_n", "gen_tokens"),
                          ("predicted_ms", "gen_ms"), ("predicted_per_second", "gen_tps")):
            if key in timings:
                value = timings[key]
                meta[name] = round(value, 3) if isinstance(value, float) else value
        return SimpleNamespace(error=error, metadata=meta)
