
# ---- the worker: ggml-rpc-server only ----
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
SHELL ["/bin/bash", "-c"]
ARG DEBIAN_SNAPSHOT=20261004T000000Z
# libgomp1: the OpenMP runtime of the CPU backend (libstdc++6 and libgcc-s1 are in the base).
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y libgomp1=12.2.0-14+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 llama \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin llama
COPY --from=llama-build /opt/llama /opt/llama
# The entrypoint (service.json init.entry_path). The guest init starts it as PID 1 and as
# root; ggml-rpc-server runs as the service user, with one thread for each vCPU of the
# instance. It listens on all the addresses of the instance on port 50052 (the slot of
# service.json): the worker declares no network, and the node lets only the parent
# instance reach the port of a child (network.ISOLATE_INTERNAL_CHILDREN). The llama.cpp
# RPC protocol has no authentication.
RUN printf '%s\n' \
        '#!/bin/sh' \
        'export LD_LIBRARY_PATH=/opt/llama/bin' \
        'exec setpriv --reuid 10001 --regid 10001 --clear-groups /opt/llama/bin/ggml-rpc-server -H 0.0.0.0 -p 50052 -t "$(nproc)"' \
        > /entrypoint.sh \
    && chmod 0755 /entrypoint.sh
# Build-time smoke test: the server starts as the service user and accepts a connection.
RUN (LD_LIBRARY_PATH=/opt/llama/bin setpriv --reuid 10001 --regid 10001 --clear-groups \
        /opt/llama/bin/ggml-rpc-server -H 127.0.0.1 -p 50052 -t 1 > /tmp/rpc.log 2>&1 &) \
    && for i in $(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/50052) 2>/dev/null && break; sleep 0.2; done \
    && (exec 3<>/dev/tcp/127.0.0.1/50052) && grep -q 'CPU' /tmp/rpc.log && echo 'smoke test: rpc-server ok' \
    && rm -f /tmp/rpc.log
