"""Serve the E2E provider's inference and control apps on separate ports."""

from __future__ import annotations

import asyncio
import os
import signal

import uvicorn

from provider import ScenarioStore, create_control_app, create_inference_app


def _required_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


async def _serve() -> None:
    control_token = _required_secret("E2E_CONTROL_TOKEN")
    inference_api_key = _required_secret("E2E_INFERENCE_API_KEY")
    inference_port = int(os.environ.get("E2E_INFERENCE_PORT", "8000"))
    control_port = int(os.environ.get("E2E_CONTROL_PORT", "8001"))

    store = ScenarioStore()
    inference_app = create_inference_app(store, inference_api_key=inference_api_key)
    control_app = create_control_app(store, control_token=control_token)

    servers = [
        uvicorn.Server(
            uvicorn.Config(
                inference_app,
                host="0.0.0.0",
                port=inference_port,
                access_log=False,
                server_header=False,
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                control_app,
                host="127.0.0.1",
                port=control_port,
                access_log=False,
                server_header=False,
            )
        ),
    ]

    # Uvicorn normally owns process signal handlers.  Two Server instances in one
    # loop would overwrite each other, so one shared handler stops both.
    for server in servers:
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()

    def stop() -> None:
        stop_requested.set()
        for server in servers:
            server.should_exit = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop)

    tasks = [asyncio.create_task(server.serve()) for server in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    unexpected_listener_exit = not stop_requested.is_set()
    stop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]

    if (
        errors
        or unexpected_listener_exit
        or not all(server.started for server in servers)
    ):
        error = RuntimeError("one or more E2E provider listeners stopped unexpectedly")
        if errors:
            raise error from errors[0]
        raise error


if __name__ == "__main__":
    asyncio.run(_serve())
