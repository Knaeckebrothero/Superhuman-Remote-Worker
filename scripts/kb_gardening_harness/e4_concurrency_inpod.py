"""E4 — concurrency repro against the k3d orchestrator, run INSIDE the
orchestrator pod (python3 - < this file), where MCP_INTERNAL_KEY is in env.

Usage: PROJECT=<uuid> python3 e4_concurrency_inpod.py
Prints one line per step; final line is a JSON summary.
"""

import hashlib
import json
import os
import threading
import time
import urllib.request

BASE = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8085").rstrip("/")
KEY = os.environ["MCP_INTERNAL_KEY"]
PROJECT = os.environ["PROJECT"]
TS = int(time.time())
SLUG = f"e4-race-{TS}"


def blob_sha(text):
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def note(version):
    return (
        f'---\nid: {SLUG}\ntype: state\ndescription: "E4 concurrency probe"\n'
        f"tags: [e4, probe]\nstatus: active\ncreated: 2026-09-02T19:00:00+00:00\n"
        f"modified: 2026-09-02T19:0{version}:00+00:00\n---\n# E4 probe\n\nversion {version}\n"
    )


def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Internal-Key": KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def mat(content, expected=None):
    body = {"slug": SLUG, "content": content}
    if expected:
        body["expected_blob_sha"] = expected
    return post(f"/api/projects/{PROJECT}/knowledge/materialize", body)


def delete(expected=None, reason="e4 probe"):
    body = {"slug": SLUG, "reason": reason}
    if expected:
        body["expected_blob_sha"] = expected
    return post(f"/api/projects/{PROJECT}/knowledge/delete", body)


def race(fn_a, fn_b):
    out = [None, None]

    def run(i, fn):
        try:
            out[i] = fn()
        except Exception as e:  # noqa: BLE001
            out[i] = {"status": "exception", "reason": repr(e)[:120]}

    ta, tb = (
        threading.Thread(target=run, args=(0, fn_a)),
        threading.Thread(target=run, args=(1, fn_b)),
    )
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    return out


summary = {}
v1 = note(1)
r = mat(v1)
print(
    "S1 create v1:",
    r.get("status"),
    r.get("reason"),
    "indexed=",
    r.get("indexed"),
    r.get("index_reason"),
)
summary["create"] = r.get("status")
sha1 = blob_sha(v1)

# S2 — two concurrent rewrites carrying the SAME token (both read v1)
a, b = race(lambda: mat(note(2), sha1), lambda: mat(note(3), sha1))
outcomes = sorted([(x.get("status"), x.get("reason")) for x in (a, b)])
print("S2 concurrent CAS updates:", outcomes)
summary["cas_race"] = outcomes
committed = [x for x in (a, b) if x.get("status") == "committed"]
# which content won? recompute expected from the winner
current = note(2) if a.get("status") == "committed" else note(3)
sha_cur = blob_sha(current)

# S3 — same race WITHOUT tokens (the pre-G3 behaviour): both should "win"
a, b = race(lambda: mat(note(4)), lambda: mat(note(5)))
outcomes = sorted([(x.get("status"), x.get("reason")) for x in (a, b)])
print("S3 concurrent unconditional updates:", outcomes)
summary["lww_race"] = outcomes
current = note(5)  # unknown which landed last; refresh token via a no-op probe below

# S4 — stale token after the LWW race: a writer that read v2 must be refused
r = mat(note(6), sha_cur)
print("S4 stale-token rewrite:", r.get("status"), r.get("reason"))
summary["stale_token"] = (r.get("status"), r.get("reason"))

# S5 — learn the live token by writing unconditionally (last writer wins), then delete with CAS
v7 = note(7)
r = mat(v7)
sha7 = blob_sha(v7)
r = delete(expected="0" * 40)
print("S5a delete with WRONG token:", r.get("status"), r.get("reason"))
summary["delete_wrong_token"] = (r.get("status"), r.get("reason"))
r = delete(expected=sha7)
print(
    "S5b delete with RIGHT token:",
    r.get("status"),
    r.get("reason"),
    "row_deleted=",
    r.get("row_deleted"),
)
summary["delete_cas"] = (r.get("status"), r.get("reason"), r.get("row_deleted"))

# S6 — the resurrection hole: a rewrite carrying the pre-delete token must NOT re-create the file
r = mat(note(8), sha7)
print("S6 stale rewrite after delete:", r.get("status"), r.get("reason"))
summary["no_resurrect"] = (r.get("status"), r.get("reason"))

# S7 — idempotent delete
r = delete(expected=None)
print("S7 delete again:", r.get("status"), r.get("reason"))
summary["delete_idempotent"] = (r.get("status"), r.get("reason"))

# S8 — an UNCONDITIONAL rewrite after delete re-creates (documented: no token = old behaviour)
r = mat(note(9))
print(
    "S8 unconditional rewrite after delete:",
    r.get("status"),
    r.get("reason"),
    r.get("operation"),
)
summary["unconditional_recreates"] = (r.get("status"), r.get("operation"))
r = delete(expected=None, reason="e4 cleanup")
print("S9 cleanup delete:", r.get("status"), r.get("reason"))
print("SUMMARY", json.dumps({"slug": SLUG, **summary}))
