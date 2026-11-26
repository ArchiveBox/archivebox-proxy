# ArchiveBox Proxy

A [Proxelar](https://github.com/emanuele-em/proxelar) caching proxy that queues visited pages for [ArchiveBox](https://github.com/ArchiveBox/ArchiveBox).

Browse through the proxy to populate its cache. Navigation hooks then run normal `archivebox add --depth=0 URL` captures through a second listener backed by the same cache. Missing responses go upstream through the proxy. An optional read-only ArchiveBox collection supplies older responses before a cache miss reaches the internet.

The proxy owns its CA, cache, navigation queue, and replay index. ArchiveBox owns its collection and runs its normal capture pipeline. The proxy writes no WARC files and does not access ArchiveBox's database. Each user runs a separate proxy and data directory.

*Originally contributed by [Bruno Schroeder (@brunocek)](https://github.com/brunocek/archivebox-proxy).*

## Installation

Install [uv](https://docs.astral.sh/uv/) and [Rust through rustup](https://rustup.rs/), then build the pinned Proxelar engine:

```bash
git clone https://github.com/ArchiveBox/archivebox-proxy.git
cd archivebox-proxy
uv sync --locked
uv run python bin/build_engine.py --locked --release
uv run archivebox-proxy \
  --engine ./target/release/archivebox-proxy-engine \
  --data "$HOME/.local/share/archivebox-proxy"
```

The build helper applies the checked-in Proxelar integration patch to a pinned source revision in the dependency cache. It adds direct intercepted responses, backpressure for interception events, and the CA chain needed by browser clients. Use the helper for builds and `--clippy` checks; `cargo fmt --check` checks the local Rust source.

The visitor listener defaults to `127.0.0.1:8080`, and the ArchiveBox worker listener to `127.0.0.1:8082`. Both use the same independent cache. For automatic capture, install ArchiveBox normally, initialize a collection, and add:

```bash
uv run archivebox-proxy \
  --engine ./target/release/archivebox-proxy-engine \
  --data "$HOME/.local/share/archivebox-proxy" \
  --archivebox-dir "$HOME/archivebox" \
  --archive "$HOME/archivebox"
```

`--archivebox-dir` selects the working directory for the normal ArchiveBox CLI hook. `--archive` independently enables read-only archive fallback. `--archivebox-command '["uv","tool","run","archivebox"]'` can select a different CLI command prefix. Custom hooks use literal argument arrays, for example `--hook '["my-command","{url}"]'`; repeat the option to run multiple commands. Navigation logging is always enabled in `navigations.jsonl`.

Docker Compose runs the standalone proxy with a persistent cache volume:

```bash
docker compose up --build -d
docker compose cp proxy:/cache/ca/proxelar-ca.pem ./proxelar-ca.pem
```

Set `ARCHIVEBOX_DATA` to an existing collection and add `-f compose.archive.yaml` to mount it read-only for fallback. Add `-f compose.capture.yaml` instead to run ArchiveBox's CLI from its official image and enable automatic captures. The base file must come first: `docker compose -f compose.yaml -f compose.capture.yaml up --build -d`.

### Debian

Native builds also require `clang`, `cmake`, `libclang-dev`, and `pkg-config`. For systemd, install the project at `/opt/archivebox-proxy`, copy the release engine to `/usr/local/bin/archivebox-proxy-engine`, and install `archivebox-proxy.service`. The unit keeps its state in `/var/lib/archivebox-proxy` and runs the standalone cache under a dynamic service user. Set the service user and collection permissions when enabling an ArchiveBox CLI hook.

## Configuring an HTTPS Client

Configure the client HTTP and HTTPS proxy to the visitor listener, then trust `DATA/ca/proxelar-ca.pem` as a certificate authority. Share only the certificate; the CA private key stays in the proxy data directory. Hook processes receive the worker proxy URL and CA trust configuration automatically, including curl, requests, wget, Node, and ArchiveBox's Chromium arguments.

For access from another device, choose the listener with `--bind` and expose that address through your network configuration. Docker binds to loopback by default; `PROXY_BIND` controls the published address. Certificate-pinned applications need their own trust configuration.

## Modes

`--mode` selects Proxelar's visitor transport. The worker listener remains an HTTP forward proxy in every mode.

| Mode | Configuration |
| --- | --- |
| `forward` | HTTP forward proxy with HTTPS interception; default |
| `socks5` | SOCKS5 listener with HTTP/HTTPS interception |
| `reverse` | `--target https://upstream-host`; native HTTP/3 support |
| `wireguard` | `--wireguard-endpoint host:port`; import `DATA/ca/proxelar-wg.conf` on the client |
| `dns` | DNS listener; upstream set with `--dns-upstream IP:port` |
| `udp` | UDP/QUIC forwarding to `--target IP:port` |

The HTTP/3 feature is built into the engine. DNS, WebSocket frames, and other native transport events are recorded in `events.jsonl`; response replay applies to completed HTTP exchanges. Long-lived upgraded streams continue through Proxelar.

`--policy` controls HTTP cache lookup:

| Policy | Behavior |
| --- | --- |
| `auto` | Fresh cache, then archive, then upstream; default |
| `record` | Always upstream, replacing matching cache entries |
| `replay` | Cache including expired entries, then archive, then upstream |
| `offline` | Cache including expired entries, then archive; HTTP misses return 504 |

`offline` controls intercepted HTTP requests. It is not a firewall for DNS, raw UDP, or other forwarded protocols.

Completed responses, including POST results and HTTP errors, stay fresh for at least 24 hours after capture. Longer origin freshness is retained. Request/response `no-store` and cookies do not prevent caching. Keys include the method, exact URL, request body, Range, Accept-Encoding, and Content-Type; cookies do not partition this single-user cache. Cache hits do not reset expiry.

Bodies are content-addressed on disk, with SQLite metadata and a default 50 GiB LRU budget (`--cache-gb`). The default capture limit is 64 MiB per request/response body (`--body-limit` in bytes); truncated exchanges are forwarded and logged but never replayed as complete responses. Conditional 304 responses do not replace an existing response body.

## Comments

### Identifying User HTTP Requests - not trivial

A top-level navigation has `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Such requests on the visitor listener append to `navigations.jsonl` and enter the durable queue. Hooks start after ten seconds, allowing the original visit to populate the cache, and are deduplicated by URL for 24 hours. The worker listener suppresses navigation hooks to prevent recursive captures.

Hooks run sequentially as subprocesses. `hooks.jsonl` records starts, exit codes, and errors; `hooks.log` contains command output. Pending queue entries survive restarts. A hook interrupted after starting remains recorded for inspection because its side effects may already have committed.

Archive fallback indexes `responses/index.jsonl` and CDX/CDXJ entries inside `archivewebpage/*.wacz`. Its derived index lives in the proxy data directory and refreshes as captures change. WACZ replay reads the indexed WARC byte range and preserves response headers and encoded bodies; POSTs must match the original paired request body. Loose `responses/` files support GET 200 replay from their recorded MIME type and decoded body. Missing, corrupt, or incompatible records fall through to the next source or upstream according to policy.

Run `uv run pytest -v` after building the debug engine with `uv run python bin/build_engine.py --locked`. Integration tests use real HTTPS and HTTP/3 origins, normal CLI subprocesses, persisted caches, and existing ArchiveBox capture fixtures.

### historic

2024-01: Bruno Schroeder contributed the original mitmproxy navigation recorder.

2026-11: Rewritten around Proxelar, an independent response cache, asynchronous navigation hooks, and read-only ArchiveBox replay.
