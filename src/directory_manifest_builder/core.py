"""Core manifest-building logic.

A manifest is a mapping from POSIX-style relative file paths to content hashes.
We hash the *contents* of each file, not its path or metadata, so that renaming
or moving a file within the tree is detectable as a content change rather than
a structural one. Directories themselves are not hashed; only regular files
appear in the manifest. This keeps the manifest stable across operating systems
that disagree about directory entry ordering.

Hashing uses SHA-256 from the standard library so the library has no
dependencies and produces values comparable across platforms.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional


__all__ = ["ManifestBuilder", "ManifestError", "build_manifest"]


class ManifestError(Exception):
    """Raised when the manifest cannot be built.

    A dedicated error type lets callers distinguish manifest failures from
    unrelated exceptions (e.g. a stray ``KeyboardInterrupt``) without inspecting
    messages.
    """


@dataclass(frozen=True)
class ManifestBuilder:
    """Build a path-to-hash manifest for a directory tree.

    Attributes:
        root: Directory to scan. Must exist and be a directory.
        hash_name: Hash algorithm name accepted by ``hashlib.new``. Defaults to
            ``"sha256"``.
        chunk_size: Bytes read per chunk when hashing. Tuned to avoid reading
            large files entirely into memory.
        follow_symlinks: When ``True``, symlinks to files are hashed via their
            target. When ``False`` (the default), symlinks are skipped. We skip
            rather than hash the link itself because hashing a link's target
            path would couple the manifest to the filesystem layout outside the
            tree, which defeats reproducibility.
        exclude_names: Basenames to exclude from the walk (e.g. ``{".git"}``).
            Excluded entries are pruned from the walk so their subtrees are not
            descended into.
        opener: Callable returning a binary file object for a path. Injected so
            tests can simulate read failures without touching the disk.
    """

    root: str
    hash_name: str = "sha256"
    chunk_size: int = 65536
    follow_symlinks: bool = False
    exclude_names: frozenset = frozenset()
    opener: Callable[[str], "object"] = open

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root:
            raise ManifestError("root must be a non-empty string")
        if not isinstance(self.hash_name, str) or not self.hash_name:
            raise ManifestError("hash_name must be a non-empty string")
        if not isinstance(self.chunk_size, int) or self.chunk_size <= 0:
            raise ManifestError("chunk_size must be a positive integer")
        if not isinstance(self.follow_symlinks, bool):
            raise ManifestError("follow_symlinks must be a bool")
        if not isinstance(self.exclude_names, (set, frozenset)):
            raise ManifestError("exclude_names must be a set or frozenset")
        # Normalise to frozenset so the dataclass stays hashable and immutable.
        if not isinstance(self.exclude_names, frozenset):
            object.__setattr__(self, "exclude_names", frozenset(self.exclude_names))
        # Validate the hash name eagerly so a bad algorithm fails fast at
        # construction rather than midway through a walk.
        try:
            hashlib.new(self.hash_name)
        except (ValueError, TypeError) as exc:
            raise ManifestError(f"unsupported hash algorithm: {self.hash_name!r}") from exc

    def _iter_files(self) -> Iterator[str]:
        """Yield absolute paths of regular files under ``root``.

        Prunes excluded directory basenames in-place by mutating ``dirnames``
        as ``os.walk`` documents, which prevents descent into them.
        """
        for dirpath, dirnames, filenames in os.walk(
            self.root, followlinks=self.follow_symlinks
        ):
            dirnames[:] = [
                name for name in dirnames if name not in self.exclude_names
            ]
            for filename in filenames:
                if filename in self.exclude_names:
                    continue
                full = os.path.join(dirpath, filename)
                if os.path.islink(full) and not self.follow_symlinks:
                    continue
                if not os.path.isfile(full):
                    # Skip sockets, fifos, devices, broken symlinks, etc.
                    # ``os.path.isfile`` follows symlinks, so when
                    # ``follow_symlinks`` is True a symlink pointing at a file
                    # passes here; a dangling symlink is skipped.
                    continue
                yield full

    def _hash_file(self, path: str) -> str:
        """Return the hex digest of ``path``'s contents."""
        hasher = hashlib.new(self.hash_name)
        try:
            handle = self.opener(path, "rb")
        except OSError as exc:
            raise ManifestError(f"cannot open {path!r}: {exc}") from exc
        try:
            while True:
                block = handle.read(self.chunk_size)
                if not block:
                    break
                hasher.update(block)
        except OSError as exc:
            raise ManifestError(f"cannot read {path!r}: {exc}") from exc
        finally:
            # ``close`` is the one method every file-like object guarantees.
            # We don't use ``contextlib.closing`` to keep the dependency
            # surface to the bare standard library.
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        return hasher.hexdigest()

    def build(self) -> Dict[str, str]:
        """Build and return the manifest as a dict.

        Keys are POSIX-style relative paths (forward slashes) from ``root``.
        Values are lowercase hex digests of file contents.

        Raises:
            ManifestError: if ``root`` is missing, not a directory, or a file
                cannot be read.
        """
        if not os.path.isdir(self.root):
            raise ManifestError(f"root is not a directory: {self.root!r}")
        manifest: Dict[str, str] = {}
        for full in self._iter_files():
            rel = os.path.relpath(full, self.root)
            posix_rel = rel.replace(os.sep, "/")
            manifest[posix_rel] = self._hash_file(full)
        return manifest


def build_manifest(
    root: str,
    *,
    hash_name: str = "sha256",
    chunk_size: int = 65536,
    follow_symlinks: bool = False,
    exclude_names: Optional[set] = None,
    opener: Optional[Callable[[str], "object"]] = None,
) -> Dict[str, str]:
    """Convenience wrapper around :class:`ManifestBuilder`.

    Returns the manifest dict directly. See :class:`ManifestBuilder` for
    argument semantics.
    """
    builder = ManifestBuilder(
        root=root,
        hash_name=hash_name,
        chunk_size=chunk_size,
        follow_symlinks=follow_symlinks,
        exclude_names=exclude_names or frozenset(),
        opener=opener or open,
    )
    return builder.build()
