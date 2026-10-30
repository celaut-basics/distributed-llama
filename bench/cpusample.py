#!/usr/bin/env python3
"""Sample the CPU time of every Cloud Hypervisor process of nodo (one per instance) on this
host, once a second, until killed. One JSON line per sample:

    {"t": <unix time>, "procs": {"<id8>": <cpu seconds>, ...}}

nodo names the process of an instance `nodo-ch-<first 8 hex of the instance id>`. The
processes of nodes inside nspawn containers are visible from the host too.
"""

import json
import os
import re
import sys
import time

TICK = os.sysconf("SC_CLK_TCK")
NAME = re.compile(rb"nodo-ch-([0-9a-f]{8})")
# The host baseline (no Celaut): the llama.cpp processes themselves.
HOST = re.compile(rb"/(llama-server|ggml-rpc-server)\x00")


def sample():
    procs = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmdline = fh.read()
            m = NAME.search(cmdline)
            h = HOST.search(cmdline)
            if not m and not h:
                continue
            key = m.group(1).decode() if m else "%s-%s" % (h.group(1).decode(), pid)
            with open("/proc/%s/stat" % pid) as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            procs[key] = (int(fields[11]) + int(fields[12])) / TICK
        except OSError:
            continue
    return procs


def main():
    out = open(sys.argv[1], "a") if len(sys.argv) > 1 else sys.stdout
    while True:
        out.write(json.dumps({"t": round(time.time(), 3), "procs": sample()}) + "\n")
        out.flush()
        time.sleep(1)


if __name__ == "__main__":
    main()
