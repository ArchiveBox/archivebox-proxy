"""Real QUIC client → Proxelar → public origin, then cached worker replay."""

import asyncio

from aioquic.asyncio import QuicConnectionProtocol, connect
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from test_proxy_live import Proxy


class H3Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = H3Connection(self._quic)
        self.done = asyncio.get_running_loop().create_future()
        self.payload = bytearray()
        self.headers = []

    def quic_event_received(self, event):
        for item in self.http.handle_event(event):
            if isinstance(item, HeadersReceived):
                self.headers += item.headers
            if isinstance(item, DataReceived):
                self.payload.extend(item.data)
            if (
                isinstance(item, (DataReceived, HeadersReceived))
                and item.stream_ended
                and not self.done.done()
            ):
                self.done.set_result((self.headers, bytes(self.payload)))


async def fetch(proxy):
    config = QuicConfiguration(
        is_client=True, alpn_protocols=["h3"], server_name="cloudflare-quic.com"
    )
    config.load_verify_locations(str(proxy.data / "ca/proxelar-ca.pem"))
    async with connect(
        "127.0.0.1", proxy.port, configuration=config, create_protocol=H3Client
    ) as client:
        sid = client._quic.get_next_available_stream_id()
        client.http.send_headers(
            sid,
            [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", b"cloudflare-quic.com"),
                (b":path", b"/"),
            ],
            end_stream=True,
        )
        client.transmit()
        return await asyncio.wait_for(client.done, 30)


def test_http3_capture_and_replay(tmp_path):
    proxy = Proxy(
        tmp_path / "h3", "--mode", "reverse", "--target", "https://cloudflare-quic.com"
    )
    try:
        headers, content = asyncio.run(fetch(proxy))
        assert (b":status", b"200") in headers and b"<html" in content.lower()
        headers2, content2 = asyncio.run(fetch(proxy))
        assert content2 == content
        assert (b"x-archivebox-proxy-cache", b"HIT") in headers2
        fields, worker = proxy.fetch("https://cloudflare-quic.com/", worker=True)
        assert b"x-archivebox-proxy-cache: hit" in fields and worker == content
    finally:
        proxy.close()


def test_cancelled_http3_streams_do_not_stop_proxy(tmp_path):
    proxy = Proxy(
        tmp_path / "cancel",
        "--mode",
        "reverse",
        "--target",
        "https://cloudflare-quic.com",
    )

    async def cancel_requests():
        config = QuicConfiguration(
            is_client=True, alpn_protocols=["h3"], server_name="cloudflare-quic.com"
        )
        config.load_verify_locations(str(proxy.data / "ca/proxelar-ca.pem"))
        for _ in range(8):
            async with connect(
                "127.0.0.1", proxy.port, configuration=config, create_protocol=H3Client
            ) as client:
                stream = client._quic.get_next_available_stream_id()
                client.http.send_headers(
                    stream,
                    [
                        (b":method", b"GET"),
                        (b":scheme", b"https"),
                        (b":authority", b"cloudflare-quic.com"),
                        (b":path", b"/"),
                    ],
                    end_stream=True,
                )
                client.transmit()
                # Send a real stream cancellation after its request, as Chrome
                # does when a page navigation no longer needs an in-flight fetch.
                await asyncio.sleep(0)
                client._quic.reset_stream(stream, 0x10C)  # H3_REQUEST_CANCELLED
                client.transmit()

    try:
        _, original = asyncio.run(fetch(proxy))
        asyncio.run(cancel_requests())
        headers, content = asyncio.run(fetch(proxy))
        assert proxy.process.poll() is None
        assert content == original
        assert (b"x-archivebox-proxy-cache", b"HIT") in headers
        # Confirm each cancelled request actually reached interception; merely
        # closing connections before headers arrived would not exercise this bug.
        log = (tmp_path / "cancel.log").read_text()
        assert log.count("INFO HIT visitor ") == 9
    finally:
        proxy.close()
