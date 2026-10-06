#!/usr/bin/env python3
"""Explicit build of the optional scoped history backend; never run by the CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SOURCE_FILES = ("main.go", "go.mod", "go.sum")


def source_digest():
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        digest.update(name.encode() + b"\0" + (ROOT / "history_bridge" / name).read_bytes() + b"\0")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True, help="NEW absolute history-runtime directory beside the account store")
    parser.add_argument("--go", default="go", help="Go executable; toolchain pinned to 1.27.1")
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute() or os.path.lexists(output):
        parser.error("output must be a new absolute directory")
    import whatsapp_cli as cli
    cli.checked_path(output.parent, directory=True)
    support = cli.history_module()
    digest = source_digest()
    if digest != support.BRIDGE_SOURCE_SHA256:
        parser.error("history backend source differs from reviewed digest")
    os.umask(0o077)
    output.mkdir(mode=0o700)
    binary = output / "whatsapp-history"
    environment = os.environ.copy()
    environment["GOTOOLCHAIN"] = "go1.27.1"
    environment.update(GOSUMDB="sum.golang.org", GOPROXY="https://proxy.golang.org",
                       GONOSUMDB="", GONOPROXY="", GOPRIVATE="", GOWORK="off",
                       GOFLAGS="", GOENV="off")
    # This explicit build may download modules through Go's checksum database.
    # -mod=readonly prevents dependency drift; the runtime has no build path.
    subprocess.run([args.go, "build", "-mod=readonly", "-trimpath", "-buildvcs=false",
                    "-ldflags=-s -w -X main.sourceDigest=" + digest, "-o", str(binary), "."],
                   cwd=ROOT / "history_bridge", env=environment, check=True, timeout=600)
    binary.chmod(0o700)
    manifest = {"abi": 1, "source_sha256": digest,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "platform": [platform.system(), platform.machine()], "go": "1.27.1",
                "whatsmeow": "35ae40906e74"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"status": "built", "abi": 1, "source_sha256": digest}))


if __name__ == "__main__":
    sys.exit(main())
