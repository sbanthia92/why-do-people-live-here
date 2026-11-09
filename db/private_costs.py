"""Keep what each place costs private, on a repository that is public.

The pipeline's state lives on the `data` branch, which anyone can read if the repository is
public. Costs are not saved there in the clear. Each run encrypts its new cost rows to the
owner's public key (a sealed box: X25519 and XSalsa20-Poly1305) and saves them as one file in
`costs/`. The matching private key stays on the owner's computer, so only the owner can read
the files. The workflow holds the public key and cannot read its own output back.
"""

import base64
import json
from pathlib import Path

import asyncpg
from nacl.public import PrivateKey, PublicKey, SealedBox

DIRECTORY = "costs"
# Where the owner keeps the private key on their own computer
DEFAULT_KEY_FILE = Path.home() / ".config" / "whydopeoplelivehere" / "cost.key"


def generate_keys() -> tuple[str, str]:
    """A new (public, private) key pair, each as base64 text."""
    private = PrivateKey.generate()
    return (
        base64.b64encode(bytes(private.public_key)).decode(),
        base64.b64encode(bytes(private)).decode(),
    )


async def export_encrypted(database_url: str, state_dir: Path, public_key: str, name: str) -> int:
    """Encrypt every row in the `costs` table to `<state_dir>/costs/<name>.enc`.

    The table only holds this run's rows (it is not loaded from saved state), so each file
    is one run's costs. Returns the number of rows written; no file is made for none.
    """
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch("SELECT (to_jsonb(t))::text FROM costs t ORDER BY id")
    finally:
        await conn.close()
    if not rows:
        return 0
    box = SealedBox(PublicKey(base64.b64decode(public_key)))
    folder = state_dir / DIRECTORY
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.enc").write_bytes(box.encrypt("".join(f"{r[0]}\n" for r in rows).encode()))
    return len(rows)


def read_encrypted(state_dir: Path, private_key: str) -> list[dict]:
    """Every saved cost row, decrypted with the owner's private key."""
    box = SealedBox(PrivateKey(base64.b64decode(private_key)))
    rows: list[dict] = []
    for file in sorted((state_dir / DIRECTORY).glob("*.enc")):
        rows += [json.loads(line) for line in box.decrypt(file.read_bytes()).decode().splitlines()]
    return rows
