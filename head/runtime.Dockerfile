
# ---- the model: Qwen2.5-1.5B-Instruct, Q4_K_M GGUF (Apache-2.0), by revision and sha256 ----
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS model
ADD --checksum=sha256:6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e \
    https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf \
    /model/model.gguf

# ---- the head: the chat interface (Python, gRPC) and llama-server ----
# python:3.11-slim-bookworm, pinned by the digest of its multi-arch index. Same Debian
# (bookworm, glibc 2.36) as the build stage.
FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
SHELL ["/bin/bash", "-c"]
ARG DEBIAN_SNAPSHOT=20261004T000000Z
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
# gRPC and protobuf for the chat interface and the gateway. The stubs of app/ were made with
# grpcio-tools 1.85.1 (scripts/gen_proto.sh). bee-rpc (the gateway protocol) is pinned by
# the commit of its v0.0.3 tag, the one that nodo installs, from the GitHub archive (no git
# in the image); --no-deps, because it asks for grpcio==1.56.0.
RUN pip3 install --no-cache-dir grpcio==1.85.1 protobuf==7.36.2 typing_extensions==4.16.0 \
    && pip3 install --no-cache-dir --no-deps \
        "bee-rpc @ https://github.com/bee-rpc-protocol/bee-rpc-over-grpc-py/archive/b250bcaca191209560be7b41b872d996e8c010d2.tar.gz"
COPY --from=llama-build /opt/llama /opt/llama
COPY --from=model /model/model.gguf /opt/model/model.gguf
# service/ holds app/ and what the packer adds: the worker (__services__, __metadata__,
# __block__) and .dependencies (WORKER=<service id>).
COPY service /service
RUN chmod -R a+rX /service /opt/model
# The entrypoint (service.json init.entry_path). The guest init starts it as PID 1 and as
# root. It gives the service user a copy of /__config__ (the gateway address and the
# configuration of the start), then starts the head as that user.
RUN printf '%s\n' \
        '#!/bin/sh' \
        'install -m 0400 -o 10001 -g 10001 /__config__ /tmp/celaut-config 2>/dev/null' \
        'export CELAUT_CONFIG=/tmp/celaut-config PYTHONUNBUFFERED=1' \
        'cd /service' \
        'exec setpriv --reuid 10001 --regid 10001 --clear-groups /usr/local/bin/python3 -m app.main' \
        > /entrypoint.sh \
    && chmod 0755 /entrypoint.sh
# Build-time smoke test, as the service user: the head with no worker loads the model and
# answers one message through the chat interface.
RUN cd /service && LLAMA_WORKERS=0 LLAMA_MAX_TOKENS=8 LLAMA_THREADS=2 LLAMA_TEMPERATURE=0 \
    setpriv --reuid 10001 --regid 10001 --clear-groups /usr/local/bin/python3 -m app.smoke
