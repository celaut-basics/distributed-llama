# ---- llama.cpp, CPU only (this stage is the same text in head/ and worker/, so BuildKit
# builds it once and the two images get the same binaries) ----
# The base is pinned by the digest of the multi-arch index of debian:bookworm-slim (the pin
# of celaut-basics/echo-chat). apt reads a frozen snapshot of the Debian archive.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS llama-build
ARG DEBIAN_SNAPSHOT=20261004T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && [ "$(grep -c '^URIs: http://snapshot.debian.org/' /etc/apt/sources.list.d/debian.sources)" = 2 ] \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y \
        build-essential cmake ninja-build git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# llama.cpp v0.8.0 (2026-10-28), by commit. GGML_BACKEND_DL + GGML_CPU_ALL_VARIANTS: one
# CPU backend per x86-64 level (sse42 ... zen4, sapphirerapids), the best one is chosen at
# run time, so the image runs on any x86-64 node and uses AVX2/AVX-512 where it exists.
# GGML_RPC: the RPC backend (llama-server --rpc) and ggml-rpc-server.
ARG LLAMA_COMMIT=8642541b255bfb5dc39cd351a178be09013a55d6
RUN git init -q /src && cd /src \
    && git fetch -q --depth 1 https://github.com/ggml-org/llama.cpp.git "${LLAMA_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DGGML_NATIVE=OFF \
        -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DBUILD_SHARED_LIBS=ON \
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF \
    && cmake --build build -j "$(nproc)" --target llama-server ggml-rpc-server \
    && mkdir -p /opt/llama/bin \
    && cp -a build/bin/llama-server build/bin/ggml-rpc-server build/bin/*.so* /opt/llama/bin/ \
    && strip --strip-unneeded /opt/llama/bin/* \
    && rm -rf /src
