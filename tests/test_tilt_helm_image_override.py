"""A saved digest must not override the image Tilt just built."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("image_key", ["image.mcp", "vmController.preparation.image"])
def test_tilt_image_clears_a_saved_digest(tmp_path, image_key):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    observation = tmp_path / "helm.json"
    fake = f"#!{sys.executable}\n" + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if Path(sys.argv[0]).name == 'kubectl':
    print(sys.stdin.read())
elif args[0] == 'status':
    print(json.dumps({'info': {'status': 'deployed'}}))
elif args[:2] == ['get', 'manifest']:
    print('{}')
elif args[:2] == ['upgrade', '--install']:
    Path(os.environ['OBSERVATION']).write_text(json.dumps(args))
else:
    raise AssertionError(args)
"""
    for name in ("helm", "kubectl"):
        executable = binaries / name
        executable.write_text(fake)
        executable.chmod(0o700)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/tilt-helm-apply.sh"),
            "--set-string",
            image_key + ".digest=sha256:" + "a" * 64,
        ],
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "RELEASE_NAME": "srw",
            "CHART": "./helm",
            "NAMESPACE": "",
            "TILT_IMAGE_COUNT": "1",
            "TILT_IMAGE_0": "srw-registry:5000/candidate:tilt-fresh",
            "TILT_IMAGE_KEY_REPO_0": image_key + ".repository",
            "TILT_IMAGE_KEY_TAG_0": image_key + ".tag",
            "TILT_IMAGE_KEY_DIGEST_0": image_key + ".digest",
            "OBSERVATION": str(observation),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(observation.read_text())
    settings = {}
    for i, argument in enumerate(args[:-1]):
        if argument in ("--set", "--set-string"):
            key, value = args[i + 1].split("=", 1)
            settings[key] = value
    assert settings == {
        image_key + ".digest": "",
        image_key + ".repository": "srw-registry:5000/candidate",
        image_key + ".tag": "tilt-fresh",
    }
