#!/usr/bin/env python3
"""Read the pushed digest for an exact Tilt image map, using its local address."""

import json
import re
import subprocess
import sys


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Image inspection did not complete.")
    return json.loads(result.stdout)


def image_digest(image_map, cluster_image):
    status = command(["tilt", "get", "imagemap", image_map, "-o", "json"])["status"]
    if status.get("imageFromCluster") != cluster_image:
        raise ValueError("Tilt image changed before deployment.")
    local = status["imageFromLocal"]
    if (
        not isinstance(local, str)
        or "@" in local
        or ":" not in local.rsplit("/", 1)[-1]
    ):
        raise ValueError("Tilt local image must have a tag.")
    repository = local.rsplit(":", 1)[0]
    rows = command(["docker", "image", "inspect", local])
    if len(rows) != 1:
        raise ValueError("Tilt local image identity is ambiguous.")
    digests = {
        ref[len(repository) + 1 :]
        for ref in rows[0].get("RepoDigests", [])
        if isinstance(ref, str) and ref.startswith(repository + "@")
    }
    if len(digests) != 1:
        raise ValueError("Tilt image has no unique pushed registry digest.")
    digest = digests.pop()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Tilt image has an invalid registry digest.")
    return digest


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("Image map and cluster image are required.")
        print(image_digest(*sys.argv[1:]))
    except Exception:
        raise SystemExit("Could not verify the pushed Tilt image digest.") from None
