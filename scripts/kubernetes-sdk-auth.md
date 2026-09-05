# Installed Kubernetes SDK authentication check

The orchestrator and VM controller use generated CoreV1, CustomObjects and
CoordinationV1 clients with in-cluster credentials. `pip check` does not detect a
loader/generated-client mismatch that drops the authorization header.

`check_kubernetes_sdk_auth.py` exercises the installed SDK's configuration loader,
generated API calls and auth-header assembly. Each API family must send an
initial fake token, refresh an expired token from a temporary file, and reject a
missing-auth negative control through both explicit configuration and the role
startup path: public `load_incluster_config()` followed by a bare generated API
constructor. The latter exercises default publication and copying, including
isolation of the copied auth mapping and refresh hook. Each case starts with a
fresh default; the inherited default object is restored in `finally`, including
on failure. Only loader file paths and environment inputs are replaced with
fakes; SDK loading and copying remain real. An intercept stops before transport;
DNS and socket connections are also blocked. It prints a JSON result containing
versions and outcomes, without token values.

Run in an environment that already has the role's Kubernetes SDK installed:

```bash
python -I scripts/check_kubernetes_sdk_auth.py
python -m pytest tests/test_kubernetes_sdk_auth_smoke.py -q
```

To check a locally built image without starting its application, resolve its
immutable image ID first. Set `SDK_AUTH_IMAGE` to the local orchestrator,
orchestrator-dev or VM-controller image reference you intend to verify:

```bash
SDK_AUTH_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$SDK_AUTH_IMAGE")
docker run --rm --pull=never --network=none --workdir /tmp \
  --mount "type=bind,source=$PWD/scripts/check_kubernetes_sdk_auth.py,target=/tmp/check_kubernetes_sdk_auth.py,readonly" \
  --entrypoint python "$SDK_AUTH_IMAGE_ID" -I /tmp/check_kubernetes_sdk_auth.py
```

The three Dockerfiles run this check with `RUN --network=none` after installing
packages in the final stage. A build-only bind mount makes changes to the script
invalidate the check without shipping it in application images. Existing main
and develop image jobs therefore fail before publication without extra builds
or SDK installs. Develop image identity/rebuild inputs and Tilt's watch/fallback
lists include the script. Agent and MCP images do not install this SDK and do
not run the check.

The focused pytest cases launch a fresh isolated interpreter so other tests'
mocked Kubernetes modules cannot hide installed-SDK regressions. They deliberately
remove generated auth, disable refresh, retain an unauthorized header and bypass
the transport to prove those failures are detected. Default-path controls also
suppress publication, break copying, drop copied auth or refresh, and share a
mutable auth mapping. Restoration is checked after success and failures. The
check verifies header
construction and refresh; it does not verify cluster connectivity, TLS, RBAC or
server API compatibility. Dependency version policy remains in each role's
requirements file.
