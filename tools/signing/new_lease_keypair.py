#!/usr/bin/env python3
"""Generate (or inspect) the Ed25519 heartbeat-lease signing key.

Context (docs/SIGNING_FIX_2026-09-19.md, launch blocker B2): the client requires
an Ed25519 `lease_sig` on every /api/license/check heartbeat
(LeaseGate::verifyLeaseSignature) and the Lambda mints it in
backend/lambda_function.py::sign_lease from SSM /orion/lease_signing_key. That
parameter does not exist, and the private half of the 2026-07-17 keypair whose
PUBLIC key is pinned in LeaseGate.cpp was not found anywhere on this machine, so
a NEW keypair is required.

KEY HANDLING - the whole point of this script:
  * the PRIVATE key is written to ONE file, in a directory you name, created
    with O_EXCL and locked down to the current user (inheritance stripped);
  * the private key is NEVER printed, echoed, logged or copied anywhere else;
  * only the PUBLIC key (base64, raw 32 bytes) is printed - that value is not a
    secret: it is the constant that gets compiled into the client;
  * `--shred` overwrites and deletes the temp file once SSM has the value.

Usage (the coordinator runs these; see the EXECUTE section of the doc):

  1. generate
       python tools/signing/new_lease_keypair.py --out-dir D:/NexusVision/signing
  2. upload (the value never passes through a shell argument)
       aws ssm put-parameter --name /orion/lease_signing_key --type SecureString \
         --value file://D:/NexusVision/signing/lease_signing_key.pem --region us-east-1
  3. confirm SSM holds the key whose public half you are about to pin
       python tools/signing/new_lease_keypair.py --verify-ssm
  4. destroy the local copy
       python tools/signing/new_lease_keypair.py --shred D:/NexusVision/signing/lease_signing_key.pem
  5. paste the printed base64 into LeaseGate::leaseVerifyPublicKey() and rebuild.

The private key exists ONLY in SSM afterwards. Losing it is recoverable (rotate:
generate again, re-upload, re-pin, rebuild); leaking it is not (anyone can mint
fire leases for any license/machine until the client is rebuilt).
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

SSM_PARAM = "/orion/lease_signing_key"
REGION = "us-east-1"
DEFAULT_NAME = "lease_signing_key.pem"


def _lock_down(path: Path) -> str:
    """Restrict the file to the current user only. Returns a human summary."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return "mode 0600"
    user = os.environ.get("USERNAME") or ""
    if not user:
        return "mode 0600 (no USERNAME; check the ACL by hand)"
    # /inheritance:r drops the inherited ACEs, /grant:r replaces any ACE for the
    # user with exactly Full control. Nobody else (not even other local admins
    # via inheritance) is granted by this DACL.
    r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, text=True)
    return "ACL: %s only" % user if r.returncode == 0 else "ACL NOT restricted (icacls failed: %s)" % r.stderr.strip()[:120]


def generate(out_dir: Path, name: str, force: bool) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / name
    if dest.exists():
        if not force:
            print(f"REFUSED: {dest} already exists (pass --force to overwrite, "
                  f"or --shred it first)", file=sys.stderr)
            return 2
        shred(dest)

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)).decode()

    # O_EXCL: never write through an existing handle/symlink someone else owns.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    del pem, key
    acl = _lock_down(dest)

    print("private key PEM : %s  (%s)" % (dest, acl))
    print("                  ^ upload to SSM, then --shred it. NEVER open, print or copy it.")
    print("")
    print("PUBLIC KEY (base64, safe to publish and to commit):")
    print("    %s" % pub_b64)
    print("")
    print("1) upload the private key (value read from the file, not the command line):")
    print("     aws ssm put-parameter --name %s --type SecureString \\" % SSM_PARAM)
    print("       --value file://%s --region %s" % (dest.as_posix(), REGION))
    print("   (add --overwrite ONLY when rotating an existing parameter)")
    print("2) pin the public half in native_orion/src/LeaseGate.cpp:")
    print('     return QByteArray::fromBase64("%s");' % pub_b64)
    print("3) verify + destroy the local copy:")
    print("     python tools/signing/new_lease_keypair.py --verify-ssm")
    print("     python tools/signing/new_lease_keypair.py --shred %s" % dest.as_posix())
    return 0


def verify_ssm(param: str, region: str) -> int:
    """Read the SSM key in-process and print ONLY its public half."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    r = subprocess.run(["aws", "ssm", "get-parameter", "--name", param, "--with-decryption",
                        "--region", region, "--query", "Parameter.Value", "--output", "text"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("FAILED to read %s: %s" % (param, (r.stderr or "").strip().splitlines()[-1:]), file=sys.stderr)
        return 2
    value = r.stdout.strip()
    if "PRIVATE KEY" not in value:
        print("%s does NOT contain a PEM private key (length %d)" % (param, len(value)), file=sys.stderr)
        return 3
    key = serialization.load_pem_private_key(value.replace("\r\n", "\n").encode(), password=None)
    del value, r
    if not isinstance(key, Ed25519PrivateKey):
        print("%s is a %s, not Ed25519" % (param, type(key).__name__), file=sys.stderr)
        return 3
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)).decode()
    print("SSM %s holds an Ed25519 private key." % param)
    print("its PUBLIC key : %s" % pub_b64)
    print("LeaseGate.cpp must read exactly:")
    print('    return QByteArray::fromBase64("%s");' % pub_b64)
    return 0


def shred(path: Path) -> int:
    """Overwrite then unlink. Best effort - SSD wear levelling can retain blocks,
    which is why the file lives on a local scratch disk and dies within minutes."""
    if not path.exists():
        print("nothing to shred at %s" % path)
        return 0
    size = path.stat().st_size
    with open(path, "r+b", buffering=0) as f:
        for _ in range(3):
            f.seek(0)
            f.write(os.urandom(max(size, 1)))
            f.flush()
            os.fsync(f.fileno())
    path.unlink()
    print("shredded %s (%d bytes overwritten 3x, then deleted)" % (path, size))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="D:/NexusVision/signing",
                    help="directory for the temporary private-key file (default: %(default)s)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="file name (default: %(default)s)")
    ap.add_argument("--force", action="store_true", help="shred and replace an existing file")
    ap.add_argument("--verify-ssm", nargs="?", const=SSM_PARAM, metavar="PARAM",
                    help="read the SSM parameter and print only its PUBLIC key")
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--shred", metavar="PATH", help="overwrite and delete a private-key file")
    args = ap.parse_args(argv)

    if args.shred:
        return shred(Path(args.shred))
    if args.verify_ssm:
        return verify_ssm(args.verify_ssm, args.region)
    return generate(Path(args.out_dir), args.name, args.force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
