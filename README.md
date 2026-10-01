# Directory Manifest Builder

Generates a manifest mapping file paths to content hashes for reproducibility and change detection.

```python
import os
import tempfile
from directory_manifest_builder import ManifestBuilder, build_manifest

# build_manifest scans an existing directory tree. Create one first:
root = tempfile.mkdtemp(prefix="my_project_")
with open(os.path.join(root, "README.md"), "w") as handle:
    handle.write("hello")
os.makedirs(os.path.join(root, "src"), exist_ok=True)
with open(os.path.join(root, "src", "main.py"), "w") as handle:
    handle.write("print(0)")

manifest = build_manifest(root, exclude_names={"__pycache__"})
# {"README.md": "sha256hex...", "src/main.py": "sha256hex..."}

builder = ManifestBuilder(root, hash_name="sha256", follow_symlinks=False)
manifest = builder.build()
```

## Why

The problem is detecting when the contents of a directory tree have changed in a way that is stable across machines and operating systems. Comparing mtimes is fragile: copying a tree through a tarball, a container layer, or a network filesystem rewrites them. Comparing file paths misses content edits. Hashing file contents and keying by relative path gives a value that only changes when bytes change.

The trade-off: this hashes contents only, not metadata. A file edited and then reverted to identical bytes produces the same manifest. Renaming a file within the tree shows up as a removed key and an added key, not as a content change. That is the intended behaviour — the manifest answers "did the content change", not "did the structure change".

## Edge cases

Symlinks are skipped by default. Hashing a symlink's target path would couple the manifest to filesystem layout outside the tree, which breaks reproducibility. Pass `follow_symlinks=True` to hash symlink targets instead; dangling symlinks are still skipped because they have no readable content.

Non-regular files (sockets, fifos, devices) are skipped silently. Only regular files appear in the manifest.

Path separators in keys are always POSIX-style forward slashes, regardless of platform, so a manifest generated on Windows compares equal to one generated on Linux for the same tree.

`exclude_names` matches basenames, not paths. `{".git"}` prunes any directory or file named exactly `.git` at any depth.
