# browser-use 0.12.9 in-process CDP probe

Probed on 2026-07-20 in the workspace image built from
`docker/Dockerfile.workspace` at commit `7479303f`.

## Result

The shared-browser daemon design's CDP pre-flight gate passes against
`browser-use==0.12.9` and the image's Playwright Chromium. The probe received
a live JPEG screencast frame, acknowledged it, dispatched mouse and keyboard
input, and read the CSS viewport without opening a second CDP connection or
exposing a debugging port.

The package does not expose `browser_use.__version__`; the probe obtained the
version with `importlib.metadata.version("browser-use")`.

## Working call forms

Obtain the focused target's attached CDP session:

```python
cdp = await session.get_or_create_cdp_session()
client = cdp.cdp_client
```

The returned object is `browser_use.browser.session.CDPSession`. Both
`cdp.session_id` and `cdp.target_id` are present. Its client is a
`browser_use.browser._cdp_timeout.TimeoutWrappedCDPClient`.

Register for frames and start the screencast:

```python
def on_frame(event, event_session_id=None):
    ...

client.register.Page.screencastFrame(on_frame)
await client.send.Page.startScreencast(
    params={
        "format": "jpeg",
        "quality": 60,
        "maxWidth": 1280,
        "maxHeight": 720,
        "everyNthFrame": 2,
    },
    session_id=cdp.session_id,
)
```

The callback receives a dict-like `ScreencastFrameEvent` followed by the
attached session ID. The event contains `data`, `metadata`, and `sessionId`.
With `everyNthFrame=2`, the probe deliberately triggered several page paints;
a static page may not produce enough paints for the second frame.

Acknowledge the frame:

```python
await client.send.Page.screencastFrameAck(
    params={"sessionId": event["sessionId"]},
    session_id=cdp.session_id,
)
```

Dispatch input:

```python
await client.send.Input.dispatchMouseEvent(
    params={"type": "mouseMoved", "x": 10, "y": 10},
    session_id=cdp.session_id,
)
await client.send.Input.dispatchKeyEvent(
    params={"type": "keyDown", "key": "Shift"},
    session_id=cdp.session_id,
)
```

Stop the screencast:

```python
await client.send.Page.stopScreencast(session_id=cdp.session_id)
```

## Viewport metrics

This call works:

```python
metrics = await client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
```

The result contains:

```text
layoutViewport
visualViewport
contentSize
cssLayoutViewport
cssVisualViewport
cssContentSize
```

Both `cssLayoutViewport` and `cssVisualViewport` held
`clientWidth=1920`/`clientHeight=1080` in the probe. Task 6 can use
`cssLayoutViewport` as planned; browser-use's own recording watchdog prefers
`cssVisualViewport`, so falling back between the two is safe if needed.

## Probe evidence

The decisive output was:

```text
browser-use version: 0.12.9
FRAME OK bytes: 6332 ... event_session_id: <same as cdp.session_id>
ACK OK
INPUT OK
layout metrics keys: [..., 'cssLayoutViewport', 'cssVisualViewport', ...]
PROBE PASS
```
