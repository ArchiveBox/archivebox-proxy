# syntax=docker/dockerfile:1
ARG ARCHIVEBOX_IMAGE=archivebox/archivebox:0.9.78rc683
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

FROM python:3.13-slim-bookworm AS proxy
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates dumb-init openssl libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 911 archivebox && \
    useradd --system --uid 911 --gid 911 --create-home --home-dir /home/archivebox archivebox && \
    mkdir /cache && chown archivebox:archivebox /cache
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY archivebox_proxy ./archivebox_proxy
RUN uv sync --locked --no-dev --no-editable
COPY --from=engine /build/target/release/archivebox-proxy-engine /usr/local/bin/
COPY LICENSE patches/Proxelar.LICENSE-MIT /usr/share/licenses/archivebox-proxy/
ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/archivebox \
    PYTHONUNBUFFERED=1 \
    UV_NO_SYNC=1
EXPOSE 8080/tcp 8080/udp 8082/tcp
VOLUME ["/cache"]
STOPSIGNAL SIGTERM
# Match the official ArchiveBox image's owner so capture hooks can read the
# private CA and trust bundle without opening the cache to other users.
USER 911:911
ENTRYPOINT ["dumb-init", "--", "archivebox-proxy", "--engine", "/usr/local/bin/archivebox-proxy-engine", "--data", "/cache"]
CMD ["--bind", "0.0.0.0", "--port", "8080", "--worker-bind", "0.0.0.0", "--worker-port", "8082"]

# Reuse ArchiveBox's tested browser/plugin runtime instead of maintaining a
# second list of capture binaries here. Its CLI alone writes /data; replay
# reads the separately mounted read-only /archivebox view of that collection.
FROM ${ARCHIVEBOX_IMAGE} AS with-archivebox
USER root
WORKDIR /opt/archivebox-proxy
COPY pyproject.toml uv.lock ./
COPY archivebox_proxy ./archivebox_proxy
# Keep root's build cache separate from the upstream image's runtime uv cache,
# which ArchiveBox needs to write as UID 911 when capture hooks run.
RUN env -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/proxy-uv-cache UV_PROJECT_ENVIRONMENT=/opt/archivebox-proxy/venv uv sync --locked --python /venv/bin/python3 --no-dev --no-editable && \
    rm -rf /tmp/proxy-uv-cache && \
    install -d -o 911 -g 911 /cache
COPY --from=engine /build/target/release/archivebox-proxy-engine /usr/local/bin/
COPY LICENSE patches/Proxelar.LICENSE-MIT /usr/share/licenses/archivebox-proxy/
WORKDIR /data
EXPOSE 8080/tcp 8080/udp 8082/tcp
VOLUME ["/cache"]
HEALTHCHECK NONE
STOPSIGNAL SIGTERM
USER 911:911
ENTRYPOINT ["dumb-init", "--", "/opt/archivebox-proxy/venv/bin/archivebox-proxy", "--engine", "/usr/local/bin/archivebox-proxy-engine", "--data", "/cache"]
CMD ["--bind", "0.0.0.0", "--port", "8080", "--worker-bind", "0.0.0.0", "--worker-port", "8082"]

# Keep the default image independent of the optional ArchiveBox installation.
FROM proxy AS runtime
