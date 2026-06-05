"""S3-backed store for per-user IDE *profile blobs* that don't fit JSONB:
the code-server ``globalStorage`` bundle (license/activation state) and the
bytes of any extension Open VSX can't provide. Layout::

    s3://<bucket>/ide-profiles/<user_id>/globalStorage.tar.zst
    s3://<bucket>/ide-profiles/<user_id>/ext/<id>/<version>.tar.zst

Reuses the snapshot boto3 client (injected) — no second client/credentials.
All blocking S3 calls run in a thread. Returns False rather than raising on a
missing object so seeding can degrade gracefully.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from botocore.exceptions import ClientError
except ImportError:  # boto3 optional in some envs
    ClientError = Exception  # type: ignore[misc,assignment]

_PREFIX = "ide-profiles"


class IdeProfileStore:
    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def globalstorage_key(self, user_id: str) -> str:
        return f"{_PREFIX}/{user_id}/globalStorage.tar.zst"

    def ext_bytes_key(self, user_id: str, ext_id: str, version: str) -> str:
        return f"{_PREFIX}/{user_id}/ext/{ext_id}/{version}.tar.zst"

    async def put_globalstorage(self, user_id: str, local_path: str) -> None:
        await self._put(self.globalstorage_key(user_id), local_path)

    async def get_globalstorage(self, user_id: str, local_path: str) -> bool:
        return await self._get(self.globalstorage_key(user_id), local_path)

    async def put_ext_bytes(
        self, user_id: str, ext_id: str, version: str, local_path: str
    ) -> None:
        await self._put(self.ext_bytes_key(user_id, ext_id, version), local_path)

    async def get_ext_bytes(
        self, user_id: str, ext_id: str, version: str, local_path: str
    ) -> bool:
        return await self._get(self.ext_bytes_key(user_id, ext_id, version), local_path)

    async def ext_bytes_exists(self, user_id: str, ext_id: str, version: str) -> bool:
        def _head() -> bool:
            try:
                self._s3.head_object(
                    Bucket=self._bucket,
                    Key=self.ext_bytes_key(user_id, ext_id, version),
                )
                return True
            except ClientError:
                return False

        return await asyncio.to_thread(_head)

    async def _put(self, key: str, local_path: str) -> None:
        def _do() -> None:
            with open(local_path, "rb") as f:
                self._s3.put_object(Bucket=self._bucket, Key=key, Body=f)

        await asyncio.to_thread(_do)

    async def _get(self, key: str, local_path: str) -> bool:
        def _do() -> bool:
            try:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError:
                return False
            with open(local_path, "wb") as f:
                f.write(resp["Body"].read())
            return True

        return await asyncio.to_thread(_do)
