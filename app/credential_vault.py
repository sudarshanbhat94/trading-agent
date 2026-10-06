"""Authenticated broker-state encryption; key material never enters Git."""
import json
import os
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet


def _fernet(state_dir):
    configured = os.getenv("BROKER_CREDENTIAL_KEY")
    if configured:
        return Fernet(configured.encode("ascii"))
    # A separately protected key is excluded from ordinary broker JSON
    # backups. Operators must escrow it independently to restore credentials.
    path = Path(os.getenv("BROKER_VAULT_KEY_PATH") or str(Path(state_dir).parent / "private" / "broker.key"))
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not path.exists():
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vault-")
        try:
            with os.fdopen(fd,"wb") as out:
                out.write(Fernet.generate_key())
                out.flush()
                os.fsync(out.fileno())
            try:
                # Publish a complete key without overwriting another worker's
                # winner. A crash can never publish an empty/partial key.
                os.link(temporary,path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("broker vault key permissions are not private")
    return Fernet(path.read_bytes())


def seal(state_dir, user_id, data):
    body = json.dumps(dict(owner=int(user_id),state=data),sort_keys=True).encode()
    return dict(format="fernet-account-v1",ciphertext=_fernet(state_dir).encrypt(body).decode("ascii"))


def unseal(state_dir, user_id, envelope):
    if envelope.get("format") != "fernet-account-v1":
        raise ValueError("unsupported broker credential envelope")
    body = json.loads(_fernet(state_dir).decrypt(envelope["ciphertext"].encode("ascii")))
    if body["owner"] != int(user_id) or not isinstance(body["state"],dict):
        raise ValueError("broker credential owner mismatch")
    return body["state"]
