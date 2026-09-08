"""Workspace-side credential installation program, delivered over SSH stdin.

Only the program and destination path appear in the command. Values travel in
stdin and are retained in the session workspace as explicitly agreed for v1.
"""

INSTALL_CREDENTIAL_ENV = r"""
import json, os, pathlib, shlex, sys, tempfile

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
state = target.with_suffix('.json')
values = json.loads(state.read_text()) if state.exists() else {}
values.update(json.load(sys.stdin))
for path, contents in (
    (state, json.dumps(values)),
    (target, ''.join('export ' + key + '=' + shlex.quote(value) + '\n'
                     for key, value in values.items())),
):
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.credentials-')
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
"""
