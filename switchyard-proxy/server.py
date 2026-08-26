#!/usr/bin/env python3
"""Switchyard LLM autorouting proxy server launcher."""

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path

from switchyard_rust.server import Server


async def forward_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    """Forward a TCP stream bidirectionally between client and native server."""
    try:
        remote_reader, remote_writer = await asyncio.open_connection(
            target_host, target_port
        )
    except Exception as e:
        print(
            f"Failed to connect to backend {target_host}:{target_port}: {e}",
            file=sys.stderr,
        )
        writer.close()
        return

    async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while not r.at_eof():
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    await asyncio.gather(
        pipe(reader, remote_writer),
        pipe(remote_reader, writer),
    )


async def run_proxy(
    stop_event: asyncio.Event,
    target_port: int,
    listen_host: str,
    listen_port: int,
) -> None:
    """Run the asyncio TCP server until stop_event is set."""
    tcp_server = await asyncio.start_server(
        lambda r, w: forward_stream(r, w, "127.0.0.1", target_port),
        listen_host,
        listen_port,
    )
    print(
        f"Switchyard proxy listening on {listen_host}:{listen_port} "
        f"-> 127.0.0.1:{target_port}",
        flush=True,
    )
    await stop_event.wait()
    tcp_server.close()
    await tcp_server.wait_closed()


def main() -> None:
    default_config = (
        Path(__file__).parent / "routes.toml"
        if (Path(__file__).parent / "routes.toml").exists()
        else Path("/app/routes.toml")
    )
    config_str = os.environ.get("SWITCHYARD_CONFIG", str(default_config))
    config_path = Path(config_str).resolve()
    listen_port = int(os.environ.get("SWITCHYARD_PORT", "4000"))
    listen_host = os.environ.get("SWITCHYARD_HOST", "0.0.0.0")

    if not config_path.exists():
        print(f"Error: configuration file not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Starting native Switchyard server with config {config_path}...",
        flush=True,
    )
    native_server = Server(config_path, port=0)
    target_port = native_server.port
    print(
        f"Native Switchyard server ready on internal port {target_port}",
        flush=True,
    )

    stop_event = asyncio.Event()
    loop = asyncio.new_event_loop()
    proxy_thread = threading.Thread(
        target=lambda: loop.run_until_complete(
            run_proxy(stop_event, target_port, listen_host, listen_port)
        ),
        daemon=True,
    )
    proxy_thread.start()

    running = True

    def handle_signal(sig: int, frame: object) -> None:
        nonlocal running
        print(f"Received signal {sig}, shutting down...", flush=True)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while running:
            time.sleep(0.5)
    finally:
        print("Stopping TCP proxy...", flush=True)
        loop.call_soon_threadsafe(stop_event.set)
        proxy_thread.join(timeout=3)
        print("Closing native Switchyard server...", flush=True)
        native_server.close()
        print("Switchyard proxy stopped.", flush=True)


if __name__ == "__main__":
    main()
