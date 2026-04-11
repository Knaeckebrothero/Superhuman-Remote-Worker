"""Workspace backend implementations.

Available backends:
- RemoteBackend: SSH/SFTP to a remote VM or workspace container (requires paramiko).

The agent process never operates on its own filesystem as a workspace;
production always uses a RemoteBackend pointing at an isolated container/VM.

RemoteBackend is not imported by default to avoid requiring paramiko.
Import explicitly: from src.core.backends.remote import RemoteBackend
"""

__all__ = []
