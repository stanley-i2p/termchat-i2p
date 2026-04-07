import io
import json
import os
import shutil
import tarfile
import getpass

from nacl import pwhash, secret, utils, exceptions


FS_VAULT_VERSION = 1
FS_OPS_LIMIT = pwhash.argon2id.OPSLIMIT_MODERATE
FS_MEM_LIMIT = pwhash.argon2id.MEMLIMIT_MODERATE

FS_RUNTIME_DIRNAME = ".fs_runtime"
FS_RUNTIME_STATE_NAME = "state.json"


def fs_vault_path(base_dir: str) -> str:
    return base_dir + ".vault"


def fs_meta_path(base_dir: str) -> str:
    return base_dir + ".vault.meta"

def fs_runtime_dir(base_dir: str) -> str:
    return os.path.join(base_dir, FS_RUNTIME_DIRNAME)


def fs_runtime_state_path(base_dir: str) -> str:
    return os.path.join(fs_runtime_dir(base_dir), FS_RUNTIME_STATE_NAME)


def fs_is_encrypted(base_dir: str) -> bool:
    return os.path.exists(fs_vault_path(base_dir)) and not os.path.exists(base_dir)


def fs_derive_key(passphrase: str, salt: bytes) -> bytes:
    return pwhash.argon2id.kdf(
        secret.SecretBox.KEY_SIZE,
        passphrase.encode("utf-8"),
        salt,
        opslimit=FS_OPS_LIMIT,
        memlimit=FS_MEM_LIMIT,
    )


def fs_write_meta(meta_path: str, meta: dict):
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)


def fs_read_meta(meta_path: str) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def fs_build_tar_bytes(base_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(base_dir, arcname=os.path.basename(base_dir))
    return buf.getvalue()


def fs_extract_tar_bytes(data: bytes, target_parent: str):
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        tar.extractall(path=target_parent)


def fs_remove_plain(base_dir: str):
    if os.path.exists(base_dir):
        shutil.rmtree(base_dir, ignore_errors=True)


def fs_encrypt(base_dir: str, passphrase: str):
    if not os.path.exists(base_dir):
        raise RuntimeError(f"Base dir does not exist: {base_dir}")

    vault_path = fs_vault_path(base_dir)
    meta_path = fs_meta_path(base_dir)

    salt = utils.random(pwhash.argon2id.SALTBYTES)
    key = fs_derive_key(passphrase, salt)
    box = secret.SecretBox(key)

    tar_bytes = fs_build_tar_bytes(base_dir)
    nonce = utils.random(secret.SecretBox.NONCE_SIZE)
    enc = box.encrypt(tar_bytes, nonce)

    with open(vault_path, "wb") as f:
        f.write(enc)

    meta = {
        "version": FS_VAULT_VERSION,
        "kdf": "argon2id",
        "salt_hex": salt.hex(),
    }
    fs_write_meta(meta_path, meta)

    fs_remove_plain(base_dir)


def fs_decrypt(base_dir: str, passphrase: str):
    vault_path = fs_vault_path(base_dir)
    meta_path = fs_meta_path(base_dir)

    if not os.path.exists(vault_path):
        return

    if not os.path.exists(meta_path):
        raise RuntimeError("Filesystem vault metadata file is missing")

    meta = fs_read_meta(meta_path)
    if meta.get("version") != FS_VAULT_VERSION:
        raise RuntimeError(f"Unsupported filesystem vault version: {meta.get('version')}")

    salt = bytes.fromhex(meta["salt_hex"])
    key = fs_derive_key(passphrase, salt)
    box = secret.SecretBox(key)

    with open(vault_path, "rb") as f:
        enc = f.read()

    try:
        plain = box.decrypt(enc)
    except exceptions.CryptoError:
        raise RuntimeError("Wrong passphrase or corrupted filesystem vault")

    parent = os.path.dirname(base_dir)
    fs_extract_tar_bytes(plain, parent)
    

def fs_verify_passphrase(base_dir: str, passphrase: str) -> bool:
    meta_path = fs_meta_path(base_dir)
    vault_path = fs_vault_path(base_dir)

    if not os.path.exists(meta_path) or not os.path.exists(vault_path):
        # Vault does not exist. No verification
        return True

    try:
        meta = fs_read_meta(meta_path)
        if meta.get("version") != FS_VAULT_VERSION:
            return False

        salt = bytes.fromhex(meta["salt_hex"])
        key = fs_derive_key(passphrase, salt)
        box = secret.SecretBox(key)

        with open(vault_path, "rb") as f:
            enc = f.read()

        # Minimal decrypt to verify password is correct
        box.decrypt(enc)
        return True
    except:
        return False


def fs_decrypt_if_needed(base_dir: str) -> str | None:
    if os.path.exists(base_dir):
        return None

    if not os.path.exists(fs_vault_path(base_dir)):
        return None

    pw = getpass.getpass("Enter filesystem passphrase: ")
    fs_decrypt(base_dir, pw)
    return pw

# Instance counter helper functions
def fs_load_runtime_state(base_dir: str) -> dict:
    path = fs_runtime_state_path(base_dir)
    if not os.path.exists(path):
        return {"instances": 0}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {"instances": 0}

        if "instances" not in data:
            data["instances"] = 0

        return data
    except:
        return {"instances": 0}


def fs_save_runtime_state(base_dir: str, state: dict):
    runtime_dir = fs_runtime_dir(base_dir)
    os.makedirs(runtime_dir, exist_ok=True)

    path = fs_runtime_state_path(base_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def fs_runtime_enter(base_dir: str) -> int:
    state = fs_load_runtime_state(base_dir)
    state["instances"] = int(state.get("instances", 0)) + 1
    fs_save_runtime_state(base_dir, state)
    return state["instances"]


def fs_runtime_leave(base_dir: str) -> int:
    state = fs_load_runtime_state(base_dir)
    current = int(state.get("instances", 0))

    if current > 0:
        current -= 1

    state["instances"] = current
    fs_save_runtime_state(base_dir, state)
    return current

