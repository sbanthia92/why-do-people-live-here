"""Make the key pair that keeps cost data private. Run once, on your own computer.

Writes the private key to a file that only you can read, and prints the public key to put in
the repository's COST_PUBLIC_KEY variable. Costs are encrypted to the public key by the daily
run and can only be read with the private key, so keep a backup of that file: without it the
saved costs cannot be read.

Usage:  python scripts/cost_keys.py [--key-file PATH]
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.private_costs import DEFAULT_KEY_FILE, generate_keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args()

    if args.key_file.exists():
        print(f"{args.key_file} already exists. Not overwriting it: that would lose access to")
        print("costs saved with the old key. Move it away first if you really want a new pair.")
        return 1
    public, private = generate_keys()
    args.key_file.parent.mkdir(parents=True, exist_ok=True)
    # Create the file readable by its owner only, before the key is written to it
    fd = os.open(args.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(private + "\n")
    print(f"Private key saved to {args.key_file} (back it up; never share or commit it).")
    print()
    print("Public key (safe to share). Add it as a repository variable named COST_PUBLIC_KEY")
    print("under Settings > Secrets and variables > Actions > Variables:")
    print()
    print(public)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
