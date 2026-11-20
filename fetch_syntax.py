"""Fetch and verify pinned Tree-sitter sources from the allowed npm registry."""

import hashlib
import io
import tarfile
import urllib.request

VERSION = "0.25.0"
PACKAGES = {
    "tree-sitter": "016448ee8e2c23aed7e1155c118cd66cd026e6436f7394b3f3606d30bf9aaa4a",
    "tree-sitter-python": "63b76b3fa8181fd79eaad4abcdb21e2babcb504dbfc7710a89934fa456d26096",
}

for name, digest in PACKAGES.items():
    url = f"https://registry.npmjs.org/{name}/-/{name}-{VERSION}.tgz"
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != digest:
        raise SystemExit(f"Checksum mismatch for {name}")
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        package.extractall(name, filter="data")
