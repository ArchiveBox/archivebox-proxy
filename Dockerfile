# syntax=docker/dockerfile:1
ARG ARCHIVEBOX_IMAGE=archivebox/archivebox:dev
FROM ghcr.io/astral-sh/uv:0.11.29 AS uv
FROM rust:1.99.1-bookworm AS engine
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang cmake libclang-dev pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY src ./src
COPY bin ./bin
COPY patches ./patches
# Cargo.lock and the manifest pin the Proxelar revision, including its QUIC stack.
RUN uv run --no-project --python 3.13 python bin/build_engine.py --locked --release
COPY pyproject.toml uv.lock hatch_build.py LICENSE README.md ./
COPY archivebox_proxy ./archivebox_proxy
# Install the same self-contained platform wheel used by native installations.
# Reuse this stage's compiled engine rather than building it a second time.
# Constraints retain the tested lockfile versions when installing that wheel.
RUN PROXY_ENGINE=/build/target/release/archivebox-proxy-engine uv build --wheel --out-dir /wheels && \
    uv export --locked --no-dev --no-emit-project --output-file /wheels/constraints.txt

FROM python:3.13-slim-bookworm AS proxy
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates dumb-init libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 911 archivebox && \
    useradd --system --uid 911 --gid 911 --create-home --home-dir /home/archivebox archivebox && \
    mkdir /cache && chown archivebox:archivebox /cache
WORKDIR /app
RUN --mount=type=bind,from=engine,source=/wheels,target=/wheels \
    uv pip install --system --no-cache --constraints /wheels/constraints.txt /wheels/*.whl
COPY LICENSE patches/Proxelar.LICENSE-MIT /usr/share/licenses/archivebox-proxy/
ENV HOME=/home/archivebox \
    PYTHONUNBUFFERED=1 \
    PROXY_DATA=/cache
EXPOSE 8080/tcp 8080/udp 8082/tcp
VOLUME ["/cache"]
STOPSIGNAL SIGTERM
# Match the official ArchiveBox image's owner so capture hooks can read the
# private CA and trust bundle without opening the cache to other users.
USER 911:911
ENTRYPOINT ["dumb-init", "--", "archivebox-proxy"]
CMD ["--bind", "0.0.0.0", "--port", "8080", "--worker-bind", "0.0.0.0", "--worker-port", "8082"]

# Reuse ArchiveBox's tested browser/plugin runtime instead of maintaining a
# second list of capture binaries here. Its CLI alone writes /data; replay
# reads the separately mounted read-only /archivebox view of that collection.
FROM ${ARCHIVEBOX_IMAGE} AS with-archivebox
USER root
WORKDIR /opt/archivebox-proxy
# Keep root's build cache separate from the upstream image's runtime uv cache,
# which ArchiveBox needs to write as UID 911 when capture hooks run.
# Expose the installed CLI for compose exec without changing ArchiveBox's Python PATH.
RUN --mount=type=bind,from=engine,source=/wheels,target=/wheels \
    env -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/proxy-uv-cache uv venv --python /venv/bin/python3 /opt/archivebox-proxy/venv && \
    UV_CACHE_DIR=/tmp/proxy-uv-cache uv pip install --python /opt/archivebox-proxy/venv/bin/python --no-cache --constraints /wheels/constraints.txt /wheels/*.whl && \
    rm -rf /tmp/proxy-uv-cache && \
    ln -s /opt/archivebox-proxy/venv/bin/archivebox-proxy /usr/local/bin/archivebox-proxy && \
    install -d -o 911 -g 911 /cache
COPY LICENSE patches/Proxelar.LICENSE-MIT /usr/share/licenses/archivebox-proxy/
WORKDIR /data
ENV PROXY_DATA=/cache
EXPOSE 8080/tcp 8080/udp 8082/tcp
VOLUME ["/cache"]
HEALTHCHECK NONE
STOPSIGNAL SIGTERM
USER 911:911
ENTRYPOINT ["dumb-init", "--", "archivebox-proxy"]
CMD ["--bind", "0.0.0.0", "--port", "8080", "--worker-bind", "0.0.0.0", "--worker-port", "8082"]

# Keep the default image independent of the optional ArchiveBox installation.
FROM proxy AS runtime
