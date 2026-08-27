"""S3-backed store for per-user IDE *profile blobs* that don't fit JSONB:
the code-server ``globalStorage`` bundle (license/activation state) and the
bytes of any extension Open VSX can't provide. Layout::

    s3://<bucket>/ide-profiles/<user_id>/globalStorage.tar.zst
    s3://<bucket>/ide-profiles/<user_id>/ext/<id>/<version>.tar.zst

Reuses the snapshot boto3 client (injected) — no second client/credentials.
All blocking S3 calls run in a thread. Returns False rather than raising on a
missing object so seeding can degrade gracefully.
"""

import hashlib
import logging
import os
import re
from typing import Any

from services.blocking_effect import joined_blocking_call

logger = logging.getLogger(__name__)

try:
    from botocore.exceptions import ClientError
except ImportError:  # boto3 optional in some envs
    ClientError = Exception  # type: ignore[misc,assignment]

_PREFIX = "ide-profiles"
MAX_GLOBALSTORAGE_BLOB_BYTES = 512 * 1024 * 1024
MAX_EXTENSION_BLOB_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class IdeProfileStore:
    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def globalstorage_key(self, user_id: str, sha256: str | None = None) -> str:
        if sha256 is not None:
            return f"{_PREFIX}/{user_id}/globalStorage/{sha256}.tar.zst"
        return f"{_PREFIX}/{user_id}/globalStorage.tar.zst"

    def ext_bytes_key(
        self, user_id: str, ext_id: str, version: str, sha256: str | None = None
    ) -> str:
        if sha256 is not None:
            return f"{_PREFIX}/{user_id}/ext/{ext_id}/{version}/{sha256}.tar.zst"
        return f"{_PREFIX}/{user_id}/ext/{ext_id}/{version}.tar.zst"

    async def put_globalstorage(self, user_id: str, local_path: str) -> dict[str, Any]:
        return await self._put_content_addressed(
            lambda digest: self.globalstorage_key(user_id, digest),
            local_path,
            max_bytes=MAX_GLOBALSTORAGE_BLOB_BYTES,
        )

    async def get_globalstorage(
        self, user_id: str, local_path: str, pointer: dict[str, Any] | None = None
    ) -> bool:
        return await self._get(
            (
                pointer
                if pointer is not None
                else {"key": self.globalstorage_key(user_id)}
            ),
            local_path,
            max_bytes=MAX_GLOBALSTORAGE_BLOB_BYTES,
        )

    async def put_ext_bytes(
        self, user_id: str, ext_id: str, version: str, local_path: str
    ) -> dict[str, Any]:
        return await self._put_content_addressed(
            lambda digest: self.ext_bytes_key(user_id, ext_id, version, digest),
            local_path,
            max_bytes=MAX_EXTENSION_BLOB_BYTES,
        )

    async def get_ext_bytes(
        self,
        user_id: str,
        ext_id: str,
        version: str,
        local_path: str,
        pointer: dict[str, Any] | None = None,
    ) -> bool:
        return await self._get(
            (
                pointer
                if pointer is not None
                else {"key": self.ext_bytes_key(user_id, ext_id, version)}
            ),
            local_path,
            max_bytes=MAX_EXTENSION_BLOB_BYTES,
        )

    async def ext_bytes_exists(
        self,
        user_id: str,
        ext_id: str,
        version: str,
        pointer: dict[str, Any] | None = None,
    ) -> bool:
        def _head() -> bool:
            key = (
                pointer.get("key")
                if isinstance(pointer, dict)
                else self.ext_bytes_key(user_id, ext_id, version)
            )
            if not isinstance(key, str) or not key:
                return False
            try:
                self._s3.head_object(
                    Bucket=self._bucket,
                    Key=key,
                )
                return True
            except ClientError:
                return False

        return await joined_blocking_call(_head)

    async def _put_content_addressed(
        self, key_factory: Any, local_path: str, *, max_bytes: int
    ) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            size = os.path.getsize(local_path)
            if size < 0 or size > max_bytes:
                raise ValueError("IDE profile blob exceeds its compressed-size cap")
            digest = hashlib.sha256()
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(_READ_CHUNK_BYTES), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            key = key_factory(sha256)
            with open(local_path, "rb") as f:
                self._s3.put_object(Bucket=self._bucket, Key=key, Body=f)
            return {"version": 1, "key": key, "sha256": sha256, "size": size}

        return await joined_blocking_call(_do)

    async def _get(
        self, pointer: dict[str, Any], local_path: str, *, max_bytes: int
    ) -> bool:
        def _do() -> bool:
            key = pointer.get("key") if isinstance(pointer, dict) else None
            if not isinstance(key, str) or not key:
                return False
            try:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError:
                return False
            body = resp["Body"]
            expected_size = pointer.get("size")
            expected_sha = pointer.get("sha256")
            pointer_version = pointer.get("version")
            content_length = resp.get("ContentLength")
            try:
                if pointer_version is not None and pointer_version != 1:
                    return False
                if expected_sha is not None and (
                    not isinstance(expected_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
                ):
                    return False
                if expected_size is not None and (
                    isinstance(expected_size, bool)
                    or not isinstance(expected_size, int)
                    or not 0 <= expected_size <= max_bytes
                ):
                    return False
                if content_length is not None and int(content_length) > max_bytes:
                    return False
                total = 0
                digest = hashlib.sha256()
                with open(local_path, "wb") as f:
                    while True:
                        chunk = body.read(_READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            return False
                        digest.update(chunk)
                        f.write(chunk)
                if expected_size is not None and total != expected_size:
                    return False
                if expected_sha is not None and digest.hexdigest() != expected_sha:
                    return False
                return True
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

        try:
            ok = await joined_blocking_call(_do)
        except BaseException:
            try:
                os.unlink(local_path)
            except FileNotFoundError:
                pass
            raise
        if not ok:
            try:
                os.unlink(local_path)
            except FileNotFoundError:
                pass
        return ok
