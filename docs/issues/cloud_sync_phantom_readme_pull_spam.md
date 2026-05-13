# Cloud sync — OpenCloud lists a phantom `README.md` per session, log spammed every 15 s

## Symptom (observed 2026-05-11)

In agent test session `7d845b7e-…` syncing to OpenCloud, the cloud
poller logs the same line every 15 s for the entire session lifetime:

```
2026-05-11 10:03:31 - src.services.cloud_sync.base - DEBUG - Pull failed for dav/spaces/cebda934-6158-4a10-b6da-3e54221dc656$a5da7d99-5d7c-40bb-9bde-82f528ccb974/sessions/7d845b7e/README.md: Remote resource: /dav/spaces/.../sessions/7d845b7e/README.md not found
```

~70 lines from a single 17-minute session. The session never created a
`README.md` (no agent activity wrote one, the cockpit doesn't push
one, and the file genuinely doesn't exist on the OpenCloud side either
— a `curl PROPFIND` confirms a 404 on that path).

The puzzle is: if the file doesn't exist on the remote, why does the
sync poller try to *pull* it every cycle?

## Root cause (likely)

Walking the code:

`src/services/cloud_sync/base.py:221-258` (`pull()`):

```python
remote_files = await self._list_remote_files()
…
for item in remote_files:
    path = item.get("path", "")
    if not path or item.get("isdir"):
        continue
    if _should_ignore(path):
        continue
    etag = item.get("etag", "")
    prev_etag = self._remote_state.get(path)
    if prev_etag and etag == prev_etag:
        continue
    try:
        if self._backend:
            await self._pull_file_to_backend(path, etag)
        else:
            await self._pull_file_local(path, etag)
        pulled.append(path)
    except Exception as e:
        logger.debug("Pull failed for %s: %s", path, e)
```

`src/services/cloud_sync/opencloud.py:194-216` (`_list_remote_files`):

```python
client = await self._dav()
return await asyncio.to_thread(client.list, "/", get_info=True)
…
for item in raw:
    raw_path = item.get("path", "")
    if raw_path.startswith(self._webdav_base_path):
        rel = raw_path[len(self._webdav_base_path):]
    else:
        rel = raw_path.strip("/")
    out.append({
        "path": rel,
        "etag": item.get("etag", "") or "",
        "isdir": bool(item.get("isdir")),
    })
```

So `webdav3.client.list("/", get_info=True)` is returning an entry
that *says it's a file* (`isdir=False`) at the path
`sessions/7d845b7e/README.md`, but downloading it 404s. Two
explanations are plausible:

1. **OpenCloud spaces listing oddity.** OpenCloud spaces have a
   convention of representing the root of a space as a file-like
   object with a synthetic name when accessed via WebDAV. The webdav3
   library's `list()` may be returning that synthetic entry as if it
   were a real file. The `cebda934…$a5da7d99…` segment in the path is
   the OpenCloud space ID — characteristic of the spaces API.
2. **A previous session genuinely created the file, then it was
   deleted on the cloud side, but a stale tombstone remains in the
   listing.** Less likely — server tombstones don't usually surface
   in PROPFIND.

Without OpenCloud-side evidence (server log of what the PROPFIND
returns) it's hard to tell which. Either way, the pull-fail logging
is correctly DEBUG-level and gracefully tolerated by the algorithm —
the cycle just keeps trying because the file remains in the listing
forever.

## Impact

- **Log noise.** ~70 DEBUG lines per 17-minute session is a lot when
  you're tailing logs. At INFO level it would be unignorable; even at
  DEBUG it dominates the lifecycle log volume.
- **Wasted polling work.** Each cycle does a real HTTP request to the
  remote that always 404s. Cheap but pointless.
- **Masks real pull failures.** When a *real* file fails to pull, it
  emits the same log line — anyone debugging would see hundreds of
  README warnings and miss the genuinely interesting one.
- **Slightly bumps `prev_etag` map.** Each cycle's `pull()` doesn't
  update `self._remote_state` for failed pulls (the success-side at
  `_pull_file_local` does, the catch path doesn't), so the entry
  stays "interesting" forever — the dedup logic
  `prev_etag and etag == prev_etag` never short-circuits this case
  even when the listing returns a stable etag.

## Fix sketch (small, multiple options)

### A. Cache 404s with a tombstone TTL

Track 404'd pull paths with a short TTL (5 minutes is plenty) and
skip them in the `pull()` loop. Maintains responsiveness if the file
genuinely appears later.

```python
# At class level
self._missing_remote: dict[str, float] = {}   # path -> until_ts

# In pull(), before _pull_file_to_backend:
now = time.time()
if (until := self._missing_remote.get(path, 0)) > now:
    continue

# In the except block (only on 404 specifically):
if "not found" in str(e).lower() or _is_404(e):
    self._missing_remote[path] = now + 300
```

A handful of lines in `base.py`. Solves the symptom regardless of
which root cause is real (#1 or #2 above).

### B. Filter the synthetic listing entries in `opencloud.py`

If the entry is a known spaces oddity, drop it at the listing layer:

```python
# in _list_remote_files, while building `out`
if rel.endswith("/README.md") and "/" not in rel.rstrip("/README.md"):
    continue   # phantom space-root listing
```

…but that's a guess about what the entries actually look like — needs
inspection of the raw `client.list(...)` output to write correctly.

### C. Both, with priority on (A)

(A) is general-purpose and correct for any 404. (B) is a targeted
fix for what we suspect is a webdav3+OpenCloud quirk. Land (A) first
to stop the bleeding, file (B) as a follow-up only if the phantom
entry causes other problems.

## Suggested first step (diagnostic)

Before either fix, get one log line of what `client.list("/", get_info=True)`
actually returns for the offending path. Add a single debug print
gated on a new env var:

```python
if os.environ.get("CLOUD_SYNC_DEBUG_LIST"):
    logger.debug("RAW listing entry: %r", item)
```

…then a 30-second test session with `CLOUD_SYNC_DEBUG_LIST=1` will
show whether the synthetic-entry hypothesis is right and inform a
targeted fix. Without that, we're guessing.

## Related code

- `src/services/cloud_sync/base.py:221-258` — `pull()` loop where the
  log line is emitted (line 251)
- `src/services/cloud_sync/base.py:259-278` — `_pull_file_to_backend()`
  (the call that 404s)
- `src/services/cloud_sync/opencloud.py:194-216` — `_list_remote_files`
  (where the phantom entry enters the pipeline)
- `src/services/cloud_sync/opencloud.py:218-227` — `_download_file`
  (the actual `client.download_sync` call that returns "not found")

## Decision pending

Not fixed. Filed at user request 2026-05-11. Cosmetic in normal
operation but actively obstructs reading agent logs. The diagnostic
step (capture raw listing entries) should land before any fix so the
real shape is known.
