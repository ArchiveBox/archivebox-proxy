//! Proxelar owns every network protocol and certificate operation. The controller
//! decides replay at request interception and commits complete capture events.
use anyhow::{Context, Result, bail};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use clap::Parser;
use proxyapi::{
    DnsConfig, InterceptConfig, InterceptDecision, Proxy, ProxyConfig, ProxyEvent, ProxyMode,
    UpstreamTlsConfig, WireGuardConfig,
};
use proxyapi_models::{ProxiedRequest, ProxiedResponse};
use std::{net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::{sync::mpsc, task::JoinSet};

#[derive(Parser)]
struct Args {
    #[arg(long)]
    backend: String,
    #[arg(long)]
    ca_dir: PathBuf,
    #[arg(long, default_value = "127.0.0.1:8080")]
    bind: SocketAddr,
    #[arg(long, default_value = "forward")]
    mode: String,
    #[arg(long)]
    target: Option<String>,
    #[arg(long)]
    worker: bool,
    #[arg(long)]
    wireguard_endpoint: Option<String>,
    #[arg(long, default_value = "1.1.1.1:53")]
    dns_upstream: SocketAddr,
    #[arg(long, default_value_t = 67108864)]
    body_limit: usize,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let args = Args::parse();
    rustls::crypto::ring::default_provider()
        .install_default()
        .map_err(|_| anyhow::anyhow!("TLS provider already configured"))?;
    proxyapi::ca::Ssl::load_or_generate(&args.ca_dir)?;
    let token = std::env::var("ARCHIVEBOX_PROXY_BACKEND_TOKEN")?;
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(120))
        .build()?;
    let intercept = InterceptConfig::new();
    // Enable before binding: no requests can escape the replay policy at startup.
    intercept.set_enabled(true);
    let (events_tx, mut events_rx) = mpsc::channel(1024);
    let dns = DnsConfig::new(args.dns_upstream);
    let mode = match args.mode.as_str() {
        "forward" => ProxyMode::Forward,
        "socks5" => ProxyMode::Socks5,
        "dns" => ProxyMode::Dns { config: dns },
        "wireguard" => {
            let endpoint = args
                .wireguard_endpoint
                .as_deref()
                .context("--wireguard-endpoint required")?;
            let (config, _) = WireGuardConfig::load_or_generate(&args.ca_dir, endpoint, dns)?;
            ProxyMode::WireGuard { config }
        }
        "reverse" => ProxyMode::Reverse {
            target: args.target.context("--target required")?.parse()?,
        },
        "udp" => ProxyMode::Udp {
            target: args.target.context("--target required")?.parse()?,
        },
        _ => bail!("unknown proxy mode"),
    };
    let proxy = Proxy::new(ProxyConfig {
        addr: args.bind,
        mode,
        event_tx: events_tx,
        ca_dir: args.ca_dir,
        upstream_tls: UpstreamTlsConfig::default(),
        intercept: Some(intercept.clone()),
        body_capture_limit: Some(args.body_limit),
        replay_rx: None,
    });
    let mut jobs = JoinSet::new();
    jobs.spawn(async move {
        proxy
            .start(std::future::pending::<()>())
            .await
            .map_err(anyhow::Error::from)
    });
    let mut handlers = JoinSet::new();
    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => break,
            ended = jobs.join_next() => { ended.context("proxy stopped")???; break; },
            completed = handlers.join_next(), if !handlers.is_empty() => { completed.unwrap()??; },
            event = events_rx.recv() => {
                let event = event.context("Proxelar event channel closed")?;
                let client = client.clone();
                let url = args.backend.clone();
                let token = token.clone();
                let intercept = intercept.clone();
                // Bound outstanding controller work so sustained traffic cannot
                // create an unbounded task queue. Store failures terminate capture.
                if handlers.len() >= 128 { handlers.join_next().await.unwrap()??; }
                handlers.spawn(async move { dispatch(event, client, url, token, intercept, args.worker).await });
            }
        }
    }
    // Close admission before cancelling listeners, then persist the events they
    // already emitted. Otherwise a normal Ctrl-C loses completed captures still
    // waiting in the bounded channel even though their clients received bytes.
    events_rx.close();
    jobs.abort_all();
    while jobs.join_next().await.is_some() {}
    while let Some(event) = events_rx.recv().await {
        if let ProxyEvent::RequestIntercepted { id, .. } = event {
            let _ = intercept.resolve(
                id,
                InterceptDecision::Block {
                    status: 503,
                    body: bytes::Bytes::from_static(b"Proxy shutting down"),
                },
            );
            continue;
        }
        if handlers.len() >= 128 {
            handlers.join_next().await.unwrap()??;
        }
        handlers.spawn(dispatch(
            event,
            client.clone(),
            args.backend.clone(),
            token.clone(),
            intercept.clone(),
            args.worker,
        ));
    }
    while let Some(done) = handlers.join_next().await {
        done??;
    }
    Ok(())
}

async fn dispatch(
    event: ProxyEvent,
    client: reqwest::Client,
    backend: String,
    token: String,
    intercept: Arc<InterceptConfig>,
    worker: bool,
) -> Result<()> {
    match event {
        ProxyEvent::RequestIntercepted { id, request } => {
            let answer: serde_json::Value = client
                .post(format!("{backend}/lookup"))
                .bearer_auth(&token)
                .json(&serde_json::json!({"id": id, "request": request_json(&request)?, "worker": worker}))
                .send()
                .await?
                .error_for_status()?
                .json()
                .await?;
            let decision = if let Some(uri) = answer["replay"].as_str() {
                // Return bytes directly through the intercepted transport. URI
                // rewrites cannot redirect a reverse HTTP/3 connection away from
                // its configured origin, and replayed redirects must stay intact.
                let response = async {
                    let replay = if request.method() == "HEAD" {
                        client.head(uri)
                    } else {
                        client.get(uri)
                    }
                    .send()
                    .await?;
                    if replay
                        .headers()
                        .get("x-archivebox-proxy-cache")
                        .and_then(|v| v.to_str().ok())
                        != Some("HIT")
                    {
                        bail!("local replay response expired or failed");
                    }
                    // Metadata preserves original header bytes independently
                    // of the local HTTP server's header serialization.
                    Ok::<_, anyhow::Error>(InterceptDecision::Respond {
                        status: serde_json::from_value(answer["status"].clone())?,
                        headers: serde_json::from_value(answer["headers"].clone())?,
                        body: replay.bytes().await?,
                    })
                }
                .await;
                match response {
                    Ok(decision) => decision,
                    Err(error) => {
                        // An expired ticket or interrupted local body affects
                        // this request only. Never forward it behind an offline
                        // policy, and keep unrelated browser traffic running.
                        eprintln!("Replay failed for request {id}: {error}");
                        InterceptDecision::Block {
                            status: 504,
                            body: bytes::Bytes::from_static(b"Archive replay unavailable"),
                        }
                    }
                }
            } else if answer["block"].as_bool().unwrap_or(false) {
                InterceptDecision::Block {
                    status: 504,
                    body: bytes::Bytes::from_static(b"Archive replay miss"),
                }
            } else {
                InterceptDecision::Forward
            };
            // Browsers can cancel an HTTP/2 or QUIC stream during lookup. The
            // oneshot receiver then disappears and resolve returns false: only
            // this response is discarded. Controller failures above stay fatal.
            let _ = intercept.resolve(id, decision);
        }
        other => {
            let event = match other {
                ProxyEvent::RequestComplete {
                    id,
                    request,
                    response,
                } => {
                    serde_json::json!({"RequestComplete": {
                        "id": id,
                        "request": request_json(&request)?,
                        "response": response_json(&response)?,
                    }})
                }
                event => serde_json::to_value(event)?,
            };
            client
                .post(format!("{backend}/event"))
                .bearer_auth(token)
                // This local authenticated header identifies the listener while
                // preserving Proxelar's native event representation on disk.
                .header("X-ArchiveBox-Proxy-Worker", worker.to_string())
                .json(&event)
                .send()
                .await?
                .error_for_status()?;
        }
    }
    Ok(())
}

fn request_json(request: &ProxiedRequest) -> Result<serde_json::Value> {
    // Preserve Proxelar's native metadata serialization, but do not first turn
    // every captured byte into a JSON number. Two 64 MiB bodies otherwise produce
    // an oversized controller request and gigabytes of temporary Value objects.
    let metadata = ProxiedRequest::new_with_body_metadata(
        request.method().clone(),
        request.uri().clone(),
        request.version(),
        request.headers().clone(),
        bytes::Bytes::new(),
        request.body_metadata(),
        request.time(),
    );
    let mut value = serde_json::to_value(metadata)?;
    value["body"] = STANDARD.encode(request.body()).into();
    Ok(value)
}

fn response_json(response: &ProxiedResponse) -> Result<serde_json::Value> {
    let metadata = ProxiedResponse::new_with_body_metadata(
        response.status(),
        response.version(),
        response.headers().clone(),
        bytes::Bytes::new(),
        response.body_metadata(),
        response.time(),
    );
    let mut value = serde_json::to_value(metadata)?;
    value["body"] = STANDARD.encode(response.body()).into();
    Ok(value)
}
