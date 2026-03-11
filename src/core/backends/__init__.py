"""Workspace backend implementations.

Available backends:
- LocalBackend: Current behavior, local filesystem via pathlib.
- RemoteBackend: SSH/SFTP to a remote VM (requires paramiko).
"""

from .local import LocalBackend

__all__ = ["LocalBackend"]

# RemoteBackend is not imported by default to avoid requiring paramiko.
# Import explicitly: from src.core.backends.remote import RemoteBackend
