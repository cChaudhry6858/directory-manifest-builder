"""Tests for directory_manifest_builder.core.

All tests build small trees under ``tempfile.mkdtemp`` and clean up in
``tearDown``. No wall-clock dependence, no sleeps, no float comparisons.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import unittest

from directory_manifest_builder.core import (
    ManifestBuilder,
    ManifestError,
    build_manifest,
)


class _TreeTestCase(unittest.TestCase):
    """Base case that creates and tears down a temp directory."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="manifest_")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel: str, content: bytes) -> str:
        """Write ``content`` to ``root/rel`` and return the full path."""
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(content)
        return full


class TestHappyPath(_TreeTestCase):
    def test_single_file(self) -> None:
        self._write("a.txt", b"hello")
        manifest = build_manifest(self.root)
        expected = hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(manifest, {"a.txt": expected})

    def test_nested_paths_use_posix_separators(self) -> None:
        self._write("sub/dir/file.txt", b"data")
        manifest = build_manifest(self.root)
        key = next(iter(manifest))
        self.assertNotIn("\\", key)
        self.assertEqual(key, "sub/dir/file.txt")

    def test_empty_directory_yields_empty_manifest(self) -> None:
        self.assertEqual(build_manifest(self.root), {})

    def test_content_change_detected(self) -> None:
        path = self._write("file.txt", b"v1")
        first = build_manifest(self.root)
        with open(path, "wb") as handle:
            handle.write(b"v2")
        second = build_manifest(self.root)
        self.assertNotEqual(first["file.txt"], second["file.txt"])

    def test_same_content_same_hash(self) -> None:
        self._write("a.txt", b"identical")
        self._write("deep/b.txt", b"identical")
        manifest = build_manifest(self.root)
        self.assertEqual(manifest["a.txt"], manifest["deep/b.txt"])


class TestExclusions(_TreeTestCase):
    def test_exclude_names_prunes_subtree(self) -> None:
        self._write("keep.txt", b"keep")
        self._write(".git/config", b"ignored")
        self._write(".git/refs/head", b"ignored")
        manifest = build_manifest(self.root, exclude_names={".git"})
        self.assertIn("keep.txt", manifest)
        self.assertFalse(any(k.startswith(".git/") for k in manifest))

    def test_exclude_names_skips_matching_files(self) -> None:
        self._write("keep.txt", b"keep")
        self._write(".DS_Store", b"junk")
        manifest = build_manifest(self.root, exclude_names={".DS_Store"})
        self.assertEqual(set(manifest), {"keep.txt"})


class TestSymlinks(_TreeTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Symlink behaviour varies by OS privilege level. Skip silently on
        # platforms where we cannot create one, rather than failing.
        if not hasattr(os, "symlink"):
            self.skipTest("os.symlink unavailable")

    def _try_symlink(self, target: str, link: str) -> None:
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks on this platform")

    def test_symlink_skipped_by_default(self) -> None:
        self._write("real.txt", b"real")
        link = os.path.join(self.root, "link.txt")
        self._try_symlink(os.path.join(self.root, "real.txt"), link)
        manifest = build_manifest(self.root)
        self.assertIn("real.txt", manifest)
        self.assertNotIn("link.txt", manifest)

    def test_symlink_followed_when_requested(self) -> None:
        self._write("real.txt", b"real")
        link = os.path.join(self.root, "link.txt")
        self._try_symlink(os.path.join(self.root, "real.txt"), link)
        manifest = build_manifest(self.root, follow_symlinks=True)
        self.assertIn("real.txt", manifest)
        self.assertIn("link.txt", manifest)
        self.assertEqual(manifest["real.txt"], manifest["link.txt"])


class TestNonRegularFiles(_TreeTestCase):
    def test_empty_subdirectory_not_in_manifest(self) -> None:
        os.makedirs(os.path.join(self.root, "empty"))
        self.assertEqual(build_manifest(self.root), {})


class TestValidation(unittest.TestCase):
    def test_missing_root_raises(self) -> None:
        with self.assertRaises(ManifestError):
            build_manifest(os.path.join(tempfile.gettempdir(), "definitely_not_here_xyz"))

    def test_file_as_root_raises(self) -> None:
        handle, path = tempfile.mkstemp(prefix="manifest_file_")
        os.close(handle)
        try:
            with self.assertRaises(ManifestError):
                build_manifest(path)
        finally:
            os.unlink(path)

    def test_bad_hash_name_raises_at_construction(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBuilder(root=tempfile.gettempdir(), hash_name="not-a-real-algo")

    def test_zero_chunk_size_raises(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBuilder(root=tempfile.gettempdir(), chunk_size=0)

    def test_empty_root_string_raises(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBuilder(root="")


class TestCustomOpener(_TreeTestCase):
    """Inject a fake opener to exercise read-error handling deterministically."""

    def test_open_failure_raises_manifest_error(self) -> None:
        self._write("file.txt", b"data")

        def failing_opener(path: str, mode: str):
            raise OSError("simulated open failure")

        builder = ManifestBuilder(root=self.root, opener=failing_opener)
        with self.assertRaises(ManifestError) as ctx:
            builder.build()
        self.assertIn("simulated open failure", str(ctx.exception))

    def test_read_failure_raises_manifest_error(self) -> None:
        self._write("file.txt", b"data")

        class _BrokenRead:
            def __init__(self, path: str, mode: str) -> None:
                self._path = path

            def read(self, size: int) -> bytes:
                raise OSError("simulated read failure")

            def close(self) -> None:
                pass

        builder = ManifestBuilder(root=self.root, opener=_BrokenRead)
        with self.assertRaises(ManifestError) as ctx:
            builder.build()
        self.assertIn("simulated read failure", str(ctx.exception))

    def test_custom_opener_used_for_content(self) -> None:
        """A custom opener that returns canned bytes controls the hash."""
        self._write("file.txt", b"on-disk")

        class _StubHandle:
            def __init__(self, content: bytes) -> None:
                self._buf = io.BytesIO(content)

            def read(self, size: int) -> bytes:
                return self._buf.read(size)

            def close(self) -> None:
                self._buf.close()

        def stub_opener(path: str, mode: str) -> _StubHandle:
            return _StubHandle(b"stubbed-content")

        builder = ManifestBuilder(root=self.root, opener=stub_opener)
        manifest = builder.build()
        expected = hashlib.sha256(b"stubbed-content").hexdigest()
        self.assertEqual(manifest["file.txt"], expected)


class TestLargeFileStreaming(_TreeTestCase):
    def test_large_file_hashed_in_chunks(self) -> None:
        # Content larger than chunk_size exercises the streaming loop.
        content = b"x" * 200_000
        self._write("big.bin", content)
        manifest = build_manifest(self.root, chunk_size=1024)
        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(manifest["big.bin"], expected)


class TestAlternateHash(_TreeTestCase):
    def test_md5_algorithm(self) -> None:
        self._write("a.txt", b"hello")
        manifest = build_manifest(self.root, hash_name="md5")
        expected = hashlib.md5(b"hello").hexdigest()
        self.assertEqual(manifest["a.txt"], expected)


if __name__ == "__main__":
    unittest.main()
