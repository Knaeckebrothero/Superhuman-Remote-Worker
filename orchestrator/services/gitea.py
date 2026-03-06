"""Gitea client for workspace delivery.

Provides a GiteaClient that bootstraps an admin user on a Gitea instance
and creates per-job repositories. Agents push workspace contents to these
repos so users can browse deliverables via Gitea's web UI.

Gracefully degrades — if Gitea is unavailable, all methods return safe
defaults and the system continues without workspace delivery.
"""

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class GiteaClient:
    """Async HTTP client for Gitea API.

    Reads configuration from environment variables:
        GITEA_URL: Base URL of the Gitea instance (e.g. http://gitea:3000)
        GITEA_ADMIN_USER: Admin username to create/use
        GITEA_ADMIN_PASSWORD: Admin password
    """

    def __init__(self) -> None:
        self._url = os.environ.get("GITEA_URL", "").rstrip("/")
        self._user = os.environ.get("GITEA_ADMIN_USER", "srw")
        self._password = os.environ.get("GITEA_ADMIN_PASSWORD", "srw_gitea")
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """True if GITEA_URL is set."""
        return bool(self._url)

    @property
    def is_initialized(self) -> bool:
        """True if admin user and access are verified."""
        return self._initialized

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                auth=(self._user, self._password),
            )
        return self._client

    async def ensure_initialized(self) -> bool:
        """Bootstrap admin user and verify access.

        Creates the admin user via Gitea's sign-up API (first user becomes
        admin). If the user already exists, verifies credentials work.

        Returns:
            True if Gitea is ready, False if unavailable or setup failed.
        """
        if not self.is_configured:
            logger.info("Gitea not configured (GITEA_URL not set), workspace delivery disabled")
            return False

        client = self._get_client()

        # Check if Gitea is reachable
        try:
            resp = await client.get(f"{self._url}/api/v1/version")
            if resp.status_code != 200:
                logger.warning(f"Gitea not reachable (status {resp.status_code})")
                return False
            logger.info(f"Gitea reachable: {resp.json().get('version', 'unknown')}")
        except httpx.HTTPError as e:
            logger.warning(f"Gitea not reachable: {e}")
            return False

        # Try to authenticate with existing user
        try:
            resp = await client.get(f"{self._url}/api/v1/user")
            if resp.status_code == 200:
                logger.info(f"Gitea admin user '{self._user}' authenticated")
                self._initialized = True
                return True
        except httpx.HTTPError:
            pass

        # User doesn't exist — create via sign-up (first user = admin)
        try:
            signup_data = {
                "username": self._user,
                "email": f"{self._user}@srw.local",
                "password": self._password,
                "must_change_password": False,
                "send_notify": False,
            }
            # Use unauthenticated request for sign-up
            async with httpx.AsyncClient(timeout=30.0) as anon_client:
                resp = await anon_client.post(
                    f"{self._url}/api/v1/admin/users",
                    json=signup_data,
                    auth=(self._user, self._password),
                )

                if resp.status_code in (201, 422):
                    # 201 = created, 422 = already exists
                    if resp.status_code == 422:
                        logger.info("Admin user creation returned 422 (may already exist)")
                    else:
                        logger.info(f"Created Gitea admin user '{self._user}'")
                else:
                    # Try user registration endpoint as fallback
                    resp = await anon_client.post(
                        f"{self._url}/user/sign_up",
                        data={
                            "user_name": self._user,
                            "email": f"{self._user}@srw.local",
                            "password": self._password,
                            "retype": self._password,
                        },
                    )
                    if resp.status_code in (200, 302, 303):
                        logger.info(f"Registered Gitea user '{self._user}' via sign-up form")
                    else:
                        logger.warning(
                            f"Failed to create Gitea user (status {resp.status_code}): "
                            f"{resp.text[:200]}"
                        )
                        return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create Gitea admin user: {e}")
            return False

        # Verify access after creation
        try:
            resp = await client.get(f"{self._url}/api/v1/user")
            if resp.status_code == 200:
                self._initialized = True
                logger.info("Gitea workspace delivery initialized")
                return True
            else:
                logger.warning(f"Gitea auth verification failed (status {resp.status_code})")
                return False
        except httpx.HTTPError as e:
            logger.warning(f"Gitea auth verification failed: {e}")
            return False

    async def create_repo(self, name: str) -> Optional[str]:
        """Create a repository and return the authenticated clone URL.

        Args:
            name: Repository name (e.g. "job-abc123")

        Returns:
            Authenticated clone URL (http://user:pass@host/user/repo.git)
            or None if creation failed.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.post(
                f"{self._url}/api/v1/user/repos",
                json={
                    "name": name,
                    "private": True,
                    "auto_init": False,
                    "description": f"Workspace for {name}",
                },
            )

            if resp.status_code == 409:
                # Already exists — return the URL anyway
                logger.debug(f"Gitea repo '{name}' already exists")
            elif resp.status_code not in (200, 201):
                logger.warning(
                    f"Failed to create Gitea repo '{name}' "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return None

            # Build authenticated clone URL
            return self._build_clone_url(name)

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create Gitea repo '{name}': {e}")
            return None

    async def delete_repo(self, name: str) -> bool:
        """Delete a repository.

        Args:
            name: Repository name

        Returns:
            True if deleted, False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.delete(
                f"{self._url}/api/v1/repos/{self._user}/{name}"
            )
            if resp.status_code == 204:
                logger.info(f"Deleted Gitea repo '{name}'")
                return True
            elif resp.status_code == 404:
                logger.debug(f"Gitea repo '{name}' not found (already deleted)")
                return True
            else:
                logger.warning(
                    f"Failed to delete Gitea repo '{name}' (status {resp.status_code})"
                )
                return False
        except httpx.HTTPError as e:
            logger.warning(f"Failed to delete Gitea repo '{name}': {e}")
            return False

    def _build_clone_url(self, repo_name: str) -> str:
        """Build an authenticated clone URL.

        Embeds credentials in the URL for git push from agents.
        Internal network only — acceptable for this use case.
        """
        # Parse URL to inject credentials
        # http://gitea:3000 -> http://user:pass@gitea:3000/user/repo.git
        url = self._url
        if "://" in url:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{self._user}:{self._password}@{rest}/{self._user}/{repo_name}.git"
        return f"http://{self._user}:{self._password}@{url}/{self._user}/{repo_name}.git"

    @staticmethod
    def mask_credentials(url: str) -> str:
        """Mask credentials in a URL for safe logging.

        Args:
            url: URL potentially containing user:pass@

        Returns:
            URL with password replaced by ***
        """
        return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)

    async def get_file(self, repo_name: str, file_path: str, ref: str | None = None) -> dict | None:
        """Read a file from a repository via Gitea API.

        Args:
            repo_name: Repository name (e.g. "job-abc123")
            file_path: Path within the repo (e.g. "output/job_frozen.json")
            ref: Branch/tag/commit (defaults to repo default branch)

        Returns:
            Decoded file content dict (parsed JSON) or None if not found/failed.
        """
        if not self._initialized:
            return None

        import base64

        client = self._get_client()
        params = {"ref": ref} if ref else {}

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                params=params,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to read {file_path} from {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            data = resp.json()
            content_b64 = data.get("content", "")
            decoded = base64.b64decode(content_b64).decode("utf-8")

            import json
            return json.loads(decoded)

        except Exception as e:
            logger.warning(f"Failed to read {file_path} from {repo_name}: {e}")
            return None

    async def create_or_update_file(
        self, repo_name: str, file_path: str, content: str, message: str,
        branch: str | None = None,
    ) -> bool:
        """Create or update a file in a repository via Gitea API.

        Args:
            repo_name: Repository name (e.g. "job-abc123")
            file_path: Path within the repo (e.g. "output/job_completion.json")
            content: File content as string
            message: Commit message
            branch: Target branch (defaults to repo's default branch)

        Returns:
            True if successful, False otherwise.
        """
        if not self._initialized:
            return False

        import base64

        client = self._get_client()
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        try:
            # Check if file already exists (need SHA for update)
            params = {"ref": branch} if branch else {}
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                params=params,
            )

            payload: dict = {
                "content": content_b64,
                "message": message,
            }

            if branch:
                payload["branch"] = branch

            if resp.status_code == 200:
                # File exists — include SHA for update
                existing = resp.json()
                payload["sha"] = existing["sha"]

            resp = await client.put(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                json=payload,
            )

            if resp.status_code in (200, 201):
                return True

            logger.warning(
                f"Failed to write {file_path} to {repo_name} "
                f"(status {resp.status_code}): {resp.text[:200]}"
            )
            return False

        except Exception as e:
            logger.warning(f"Failed to write {file_path} to {repo_name}: {e}")
            return False

    async def delete_file(
        self, repo_name: str, file_path: str, message: str,
        branch: str | None = None,
    ) -> bool:
        """Delete a file from a repository via Gitea API.

        Args:
            repo_name: Repository name
            file_path: Path within the repo
            message: Commit message
            branch: Target branch (defaults to repo's default branch)

        Returns:
            True if deleted, False otherwise.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            # Get current SHA (required for delete)
            params = {"ref": branch} if branch else {}
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                params=params,
            )
            if resp.status_code == 404:
                return True  # Already gone
            if resp.status_code != 200:
                return False

            sha = resp.json()["sha"]

            delete_payload: dict = {"sha": sha, "message": message}
            if branch:
                delete_payload["branch"] = branch

            resp = await client.delete(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                json=delete_payload,
            )

            return resp.status_code == 200

        except Exception as e:
            logger.warning(f"Failed to delete {file_path} from {repo_name}: {e}")
            return False

    async def list_contents(
        self, repo_name: str, path: str = "", ref: str | None = None
    ) -> list[dict] | None:
        """List directory contents from a repository.

        Args:
            repo_name: Repository name
            path: Directory path within the repo (empty string for root)
            ref: Branch/tag/commit

        Returns:
            List of file/dir entries with name, path, type, size, or None on failure.
            Each entry has: name, path, type ("file"|"dir"|"submodule"), size.
        """
        if not self._initialized:
            return None

        client = self._get_client()
        params = {"ref": ref} if ref else {}
        url_path = f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents"
        if path:
            url_path += f"/{path}"

        try:
            resp = await client.get(url_path, params=params)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to list {path or '/'} in {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            data = resp.json()

            # Gitea returns a list for directories, a single object for files
            if isinstance(data, dict):
                # Single file — wrap in list for consistency
                return [data]

            return [
                {
                    "name": entry["name"],
                    "path": entry["path"],
                    "type": entry["type"],
                    "size": entry.get("size", 0),
                }
                for entry in data
            ]

        except Exception as e:
            logger.warning(f"Failed to list {path or '/'} in {repo_name}: {e}")
            return None

    async def get_file_content(
        self, repo_name: str, file_path: str, ref: str | None = None
    ) -> str | None:
        """Read raw file content as a string from a repository.

        Unlike get_file() which parses JSON, this returns the raw text content.

        Args:
            repo_name: Repository name
            file_path: Path within the repo
            ref: Branch/tag/commit

        Returns:
            File content as string, or None if not found/failed.
        """
        if not self._initialized:
            return None

        import base64

        client = self._get_client()
        params = {"ref": ref} if ref else {}

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
                params=params,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to read {file_path} from {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            data = resp.json()
            content_b64 = data.get("content", "")
            return base64.b64decode(content_b64).decode("utf-8")

        except Exception as e:
            logger.warning(f"Failed to read {file_path} from {repo_name}: {e}")
            return None

    async def get_commits(
        self,
        repo_name: str,
        sha: str = "main",
        page: int = 1,
        limit: int = 20,
    ) -> list[dict] | None:
        """List commits from a branch, tag, or SHA.

        Args:
            repo_name: Repository name
            sha: Branch, tag, or commit SHA to list from
            page: Page number (1-indexed)
            limit: Max commits per page

        Returns:
            List of commit dicts with sha, message, author, date, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/git/commits",
                params={"sha": sha, "page": page, "limit": limit},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to list commits for {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            commits = resp.json()
            return [
                {
                    "sha": c["sha"],
                    "message": c.get("commit", {}).get("message", ""),
                    "author": c.get("commit", {}).get("author", {}).get("name", ""),
                    "date": c.get("commit", {}).get("author", {}).get("date", ""),
                }
                for c in commits
            ]

        except Exception as e:
            logger.warning(f"Failed to list commits for {repo_name}: {e}")
            return None

    async def get_compare(
        self, repo_name: str, base: str, head: str = "HEAD"
    ) -> dict | None:
        """Compare two refs and return commits between them.

        Args:
            repo_name: Repository name
            base: Base ref (commit SHA, tag, or branch)
            head: Head ref (default: HEAD)

        Returns:
            Dict with total_commits and commits list, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/compare/{base}...{head}",
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to compare {base}...{head} in {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            data = resp.json()
            commits = [
                {
                    "sha": c["sha"],
                    "message": c.get("commit", {}).get("message", ""),
                    "author": c.get("commit", {}).get("author", {}).get("name", ""),
                    "date": c.get("commit", {}).get("author", {}).get("date", ""),
                }
                for c in data.get("commits", [])
            ]
            return {
                "total_commits": data.get("total_commits", len(commits)),
                "commits": commits,
            }

        except Exception as e:
            logger.warning(f"Failed to compare {base}...{head} in {repo_name}: {e}")
            return None

    async def get_diff(
        self, repo_name: str, base: str, head: str = "HEAD"
    ) -> str | None:
        """Get raw unified diff between two refs.

        Args:
            repo_name: Repository name
            base: Base ref (commit SHA, tag, or branch)
            head: Head ref (default: HEAD)

        Returns:
            Unified diff as text, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/compare/{base}...{head}.diff",
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to get diff {base}...{head} in {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            return resp.text

        except Exception as e:
            logger.warning(f"Failed to get diff {base}...{head} in {repo_name}: {e}")
            return None

    async def get_tags(
        self, repo_name: str, page: int = 1, limit: int = 50
    ) -> list[dict] | None:
        """List tags in a repository.

        Args:
            repo_name: Repository name
            page: Page number (1-indexed)
            limit: Max tags per page

        Returns:
            List of tag dicts with name, sha, and date, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/tags",
                params={"page": page, "limit": limit},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to list tags for {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            tags = resp.json()
            return [
                {
                    "name": t["name"],
                    "sha": t.get("id", t.get("commit", {}).get("sha", "")),
                    "message": t.get("message", ""),
                }
                for t in tags
            ]

        except Exception as e:
            logger.warning(f"Failed to list tags for {repo_name}: {e}")
            return None

    # =========================================================================
    # Branch & PR Operations (Projects support)
    # =========================================================================

    async def create_branch(
        self, repo_name: str, new_branch: str, from_branch: str = "main"
    ) -> bool:
        """Create a new branch in a repository.

        Args:
            repo_name: Repository name
            new_branch: Name for the new branch
            from_branch: Branch to create from (default: main)

        Returns:
            True if created (or already exists), False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.post(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/branches",
                json={
                    "new_branch_name": new_branch,
                    "old_branch_name": from_branch,
                },
            )

            if resp.status_code in (201, 200):
                logger.info(f"Created branch '{new_branch}' in {repo_name}")
                return True
            elif resp.status_code == 409:
                logger.debug(f"Branch '{new_branch}' already exists in {repo_name}")
                return True
            else:
                logger.warning(
                    f"Failed to create branch '{new_branch}' in {repo_name} "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create branch '{new_branch}' in {repo_name}: {e}")
            return False

    async def list_branches(self, repo_name: str) -> list[dict] | None:
        """List all branches in a repository.

        Args:
            repo_name: Repository name

        Returns:
            List of branch dicts with name and commit info, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.get(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/branches",
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning(
                    f"Failed to list branches for {repo_name} "
                    f"(status {resp.status_code})"
                )
                return None

            return [
                {
                    "name": b["name"],
                    "sha": b.get("commit", {}).get("id", ""),
                    "protected": b.get("protected", False),
                }
                for b in resp.json()
            ]

        except httpx.HTTPError as e:
            logger.warning(f"Failed to list branches for {repo_name}: {e}")
            return None

    async def delete_branch(self, repo_name: str, branch: str) -> bool:
        """Delete a branch from a repository.

        Args:
            repo_name: Repository name
            branch: Branch name to delete

        Returns:
            True if deleted (or not found), False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.delete(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/branches/{branch}",
            )
            if resp.status_code == 204:
                logger.info(f"Deleted branch '{branch}' from {repo_name}")
                return True
            elif resp.status_code == 404:
                logger.debug(f"Branch '{branch}' not found in {repo_name}")
                return True
            else:
                logger.warning(
                    f"Failed to delete branch '{branch}' from {repo_name} "
                    f"(status {resp.status_code})"
                )
                return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to delete branch '{branch}' from {repo_name}: {e}")
            return False

    async def create_pr(
        self,
        repo_name: str,
        title: str,
        head: str,
        base: str = "main",
        body: str = "",
    ) -> dict | None:
        """Create a pull request in a repository.

        Args:
            repo_name: Repository name
            title: PR title
            head: Head branch name
            base: Base branch name (default: main)
            body: PR description

        Returns:
            PR dict with number and url fields, or None on failure.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.post(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/pulls",
                json={
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body,
                },
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                logger.info(
                    f"Created PR #{data.get('number')} in {repo_name}: {title}"
                )
                return {
                    "number": data["number"],
                    "url": data.get("html_url", ""),
                    "state": data.get("state", "open"),
                }
            else:
                logger.warning(
                    f"Failed to create PR in {repo_name} "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return None

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create PR in {repo_name}: {e}")
            return None

    async def merge_pr(
        self,
        repo_name: str,
        pr_index: int,
        merge_strategy: str = "merge",
        delete_branch_after_merge: bool = False,
    ) -> bool:
        """Merge a pull request.

        Args:
            repo_name: Repository name
            pr_index: PR number/index
            merge_strategy: Merge method — "merge", "rebase", or "squash"
            delete_branch_after_merge: Delete the head branch after merge

        Returns:
            True if merged, False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.post(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/pulls/{pr_index}/merge",
                json={
                    "Do": merge_strategy,
                    "delete_branch_after_merge": delete_branch_after_merge,
                },
            )

            if resp.status_code in (200, 204):
                logger.info(
                    f"Merged PR #{pr_index} in {repo_name} (strategy: {merge_strategy})"
                )
                return True
            else:
                logger.warning(
                    f"Failed to merge PR #{pr_index} in {repo_name} "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to merge PR #{pr_index} in {repo_name}: {e}")
            return False

    async def rename_repo(self, old_name: str, new_name: str) -> bool:
        """Rename a repository.

        Args:
            old_name: Current repository name
            new_name: New repository name

        Returns:
            True if renamed, False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.patch(
                f"{self._url}/api/v1/repos/{self._user}/{old_name}",
                json={"name": new_name},
            )

            if resp.status_code == 200:
                logger.info(f"Renamed Gitea repo '{old_name}' -> '{new_name}'")
                return True
            elif resp.status_code == 404:
                logger.warning(f"Gitea repo '{old_name}' not found for rename")
                return False
            else:
                logger.warning(
                    f"Failed to rename Gitea repo '{old_name}' "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to rename Gitea repo '{old_name}': {e}")
            return False

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
