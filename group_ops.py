import base64
import gzip
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional
import fcntl

from nacl.signing import SigningKey, VerifyKey


GROUP_INVITE_STRING_PREFIX = "ICEDCOMM-GROUP-INVITE-v1:"
GROUP_CONTROL_JOIN_PROOF = "join_proof"
GROUP_CONTROL_RENAME_REQUEST = "rename_request"


GROUP_INVITE_FORMAT = "icedcomm-i2p-group-invite"
GROUP_ROSTER_FORMAT = "icedcomm-i2p-group-roster"
GROUP_ROSTER_SIGNATURE_FORMAT = "icedcomm-i2p-group-roster-signature"


def is_valid_b32_address(value: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"[a-z2-7]{52}\.b32\.i2p", value.strip().lower()) is not None


def short_b32(value: Optional[str]) -> str:
    if not value:
        return "unknown"
    return value.split(".", 1)[0][:8]


def _b64_url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_url_no_pad_decode(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def compact_json_bytes(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def group_storage_key(meta: dict) -> str:
    for key in ("id", "owner_b32", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"tmp_group_{int(time.time() * 1000)}"


def safe_group_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value.strip())[:160] or "group"


def group_self_display_name(meta: dict) -> str:
    name = str(meta.get("my_name") or "").strip()
    if name:
        return name
    return f"member-{short_b32(meta.get('my_b32'))}"


def group_is_admin(meta: dict) -> bool:
    my_b32 = str(meta.get("my_b32") or "").lower()
    owner_b32 = str(meta.get("owner_b32") or "").lower()
    return bool(my_b32 and owner_b32 and my_b32 == owner_b32)


def normalize_member(member: dict) -> dict:
    return {
        "name": str(member.get("name") or "").strip()[:32] or f"member-{short_b32(member.get('b32'))}",
        "b32": str(member.get("b32") or "").strip().lower(),
    }


def canonical_group_members(meta: dict) -> list[dict]:
    members = []
    seen = set()

    for member in meta.get("members") or []:
        normalized = normalize_member(member)
        b32_key = normalized["b32"].lower()
        if is_valid_b32_address(b32_key) and b32_key not in seen:
            seen.add(b32_key)
            members.append(normalized)

    my_b32 = str(meta.get("my_b32") or "").strip().lower()
    owner_b32 = str(meta.get("owner_b32") or "").strip().lower()
    if my_b32 and owner_b32 and my_b32 == owner_b32 and owner_b32 not in seen:
        members.append({"name": group_self_display_name(meta), "b32": owner_b32})

    members.sort(key=lambda item: (item["b32"].lower(), item["name"].lower()))
    return members


def merge_group_member(meta: dict, member: dict) -> bool:
    normalized = normalize_member(member)
    if not is_valid_b32_address(normalized["b32"]):
        return False

    my_b32 = str(meta.get("my_b32") or "").lower()
    if my_b32 and normalized["b32"].lower() == my_b32:
        return False

    members = meta.setdefault("members", [])
    for index, existing in enumerate(members):
        if str(existing.get("b32") or "").lower() == normalized["b32"].lower():
            if existing.get("name") != normalized["name"] or existing.get("b32") != normalized["b32"]:
                members[index] = normalized
                return True
            return False

    members.append(normalized)
    members.sort(key=lambda item: (str(item.get("b32") or "").lower(), str(item.get("name") or "").lower()))
    return True


def make_group_meta(name: str, my_name: str = "") -> dict:
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("group name cannot be empty")
    return {
        "id": f"tmp_{trimmed}_{time.time_ns()}",
        "name": trimmed,
        "my_dest_b64": None,
        "my_b32": None,
        "my_name": my_name.strip()[:32],
        "owner_b32": None,
        "roster_version": 1,
        "members": [],
        "join_token": None,
        "issued_invites": [],
        "roster_signing_pubkey": None,
        "roster_signing_secret": None,
        "roster_signature": None,
    }


def group_roster_signature_payload(meta: dict) -> bytes:
    owner_b32 = meta.get("owner_b32")
    if not owner_b32:
        raise ValueError("group owner address is not known")

    payload = {
        "format": GROUP_ROSTER_SIGNATURE_FORMAT,
        "version": 1,
        "group_name": meta.get("name") or "",
        "owner_b32": owner_b32,
        "roster_version": int(meta.get("roster_version") or 1),
        "members": canonical_group_members(meta),
    }
    return compact_json_bytes(payload)


def ensure_group_roster_signing_key(meta: dict) -> None:
    if meta.get("roster_signing_secret") and meta.get("roster_signing_pubkey"):
        return

    signing_key = SigningKey.generate()
    meta["roster_signing_secret"] = base64.b64encode(bytes(signing_key)).decode("ascii")
    meta["roster_signing_pubkey"] = base64.b64encode(bytes(signing_key.verify_key)).decode("ascii")


def sign_group_roster_if_admin(meta: dict) -> None:
    if not group_is_admin(meta):
        return

    ensure_group_roster_signing_key(meta)
    secret = base64.b64decode(meta["roster_signing_secret"])
    if len(secret) != 32:
        raise ValueError("group roster signing secret has invalid length")

    signing_key = SigningKey(secret)
    meta["roster_signing_pubkey"] = base64.b64encode(bytes(signing_key.verify_key)).decode("ascii")
    signature = signing_key.sign(group_roster_signature_payload(meta)).signature
    meta["roster_signature"] = base64.b64encode(signature).decode("ascii")


def verify_group_roster_signature(
    group_name: str,
    owner_b32: str,
    roster_version: int,
    members: list[dict],
    pubkey_b64: str,
    signature_b64: str,
) -> None:
    pubkey = base64.b64decode(pubkey_b64)
    signature = base64.b64decode(signature_b64)
    if len(pubkey) != 32:
        raise ValueError("group roster signing public key has invalid length")
    if len(signature) != 64:
        raise ValueError("group roster signature has invalid length")

    sorted_members = [normalize_member(member) for member in members if is_valid_b32_address(str(member.get("b32") or ""))]
    sorted_members.sort(key=lambda item: (item["b32"].lower(), item["name"].lower()))
    payload = {
        "format": GROUP_ROSTER_SIGNATURE_FORMAT,
        "version": 1,
        "group_name": group_name,
        "owner_b32": owner_b32,
        "roster_version": int(roster_version),
        "members": sorted_members,
    }
    VerifyKey(pubkey).verify(compact_json_bytes(payload), signature)


def generate_group_invite_token() -> str:
    return _b64_url_no_pad(os.urandom(32))


def group_invite_from_meta(meta: dict, invite_token: Optional[str] = None) -> dict:
    inviter_b32 = meta.get("my_b32")
    if not inviter_b32:
        raise ValueError("open this group once before exporting its invite")

    return {
        "format": GROUP_INVITE_FORMAT,
        "version": 1,
        "group_name": meta.get("name") or "",
        "inviter_name": group_self_display_name(meta),
        "inviter_b32": inviter_b32,
        "owner_b32": meta.get("owner_b32") or inviter_b32,
        "invite_token": invite_token,
        "roster_version": int(meta.get("roster_version") or 1),
        "members": list(meta.get("members") or []),
        "roster_signing_pubkey": meta.get("roster_signing_pubkey"),
        "roster_signature": meta.get("roster_signature"),
    }


def issue_group_invite(meta: dict) -> tuple[dict, str]:
    if not group_is_admin(meta):
        raise ValueError("only the group admin can issue invites")

    token = generate_group_invite_token()
    updated = dict(meta)
    updated["members"] = list(meta.get("members") or [])
    updated["issued_invites"] = list(meta.get("issued_invites") or [])
    sign_group_roster_if_admin(updated)
    updated["issued_invites"].append({"token": token, "redeemed_b32": None})
    invite = group_invite_from_meta(updated, token)
    encoded = _b64_url_no_pad(gzip.compress(compact_json_bytes(invite)))
    return updated, f"{GROUP_INVITE_STRING_PREFIX}{encoded}"


def decode_group_invite_string(value: str) -> dict:
    encoded = value.strip()
    if not encoded.startswith(GROUP_INVITE_STRING_PREFIX):
        raise ValueError("invite string has wrong prefix")
    compressed = _b64_url_no_pad_decode(encoded[len(GROUP_INVITE_STRING_PREFIX):])
    invite = json.loads(gzip.decompress(compressed).decode("utf-8"))
    if invite.get("format") != GROUP_INVITE_FORMAT or invite.get("version") != 1:
        raise ValueError("unsupported group invite")
    return invite


def roster_sync_from_meta(meta: dict) -> dict:
    owner_b32 = meta.get("owner_b32")
    pubkey = meta.get("roster_signing_pubkey")
    signature = meta.get("roster_signature")
    if not owner_b32 or not pubkey or not signature:
        raise ValueError("group roster is not signed")

    return {
        "format": GROUP_ROSTER_FORMAT,
        "version": 1,
        "group_name": meta.get("name") or "",
        "owner_b32": owner_b32,
        "roster_version": int(meta.get("roster_version") or 1),
        "members": canonical_group_members(meta),
        "roster_signing_pubkey": pubkey,
        "roster_signature": signature,
    }


def build_group_control(kind: str, token: str, b32: str, name: str) -> dict:
    return {
        "kind": kind,
        "token": token,
        "b32": b32,
        "name": name,
    }


def redeem_group_invite_token(meta: dict, token: str, member: dict) -> None:
    if not group_is_admin(meta):
        raise ValueError("only the group admin can redeem invites")

    for invite in meta.get("issued_invites") or []:
        if invite.get("token") == token:
            redeemed = invite.get("redeemed_b32")
            member_b32 = normalize_member(member)["b32"]
            if redeemed and redeemed.lower() != member_b32.lower():
                raise ValueError("group invite token already redeemed")
            invite["redeemed_b32"] = member_b32
            changed = merge_group_member(meta, member)
            if changed:
                meta["roster_version"] = int(meta.get("roster_version") or 1) + 1
            sign_group_roster_if_admin(meta)
            return

    raise ValueError("unknown group invite token")


def merge_group_invite(meta: dict, invite: dict) -> None:
    if invite.get("format") != GROUP_INVITE_FORMAT or invite.get("version") != 1:
        raise ValueError("unsupported group invite")
    if not is_valid_b32_address(invite.get("inviter_b32") or ""):
        raise ValueError("invite inviter address is invalid")

    owner_b32 = (invite.get("owner_b32") or invite.get("inviter_b32") or "").lower()
    if not is_valid_b32_address(owner_b32):
        raise ValueError("invite owner address is invalid")

    meta["name"] = invite.get("group_name") or meta.get("name") or "group"
    meta["owner_b32"] = owner_b32
    meta["id"] = owner_b32

    incoming_members = [{"name": invite.get("inviter_name") or f"member-{short_b32(invite.get('inviter_b32'))}", "b32": invite.get("inviter_b32")}]
    incoming_members.extend(invite.get("members") or [])
    incoming_members = [normalize_member(member) for member in incoming_members if is_valid_b32_address(str(member.get("b32") or ""))]

    pubkey = invite.get("roster_signing_pubkey")
    signature = invite.get("roster_signature")
    incoming_version = int(invite.get("roster_version") or 1)
    if pubkey and signature:
        if not any(member["b32"].lower() == owner_b32.lower() for member in incoming_members):
            raise ValueError("signed invite roster does not contain the group owner")
        existing_pubkey = meta.get("roster_signing_pubkey")
        if existing_pubkey and existing_pubkey != pubkey:
            raise ValueError("invite roster signing key does not match stored group key")
        verify_group_roster_signature(meta["name"], owner_b32, incoming_version, incoming_members, pubkey, signature)
        meta["roster_signing_pubkey"] = pubkey
        meta["roster_signature"] = signature
    elif meta.get("roster_signing_pubkey"):
        raise ValueError("incoming invite roster is unsigned")

    if incoming_version > int(meta.get("roster_version") or 1):
        meta["members"] = []

    for member in incoming_members:
        merge_group_member(meta, member)

    meta["roster_version"] = max(int(meta.get("roster_version") or 1), incoming_version)
    if invite.get("invite_token"):
        meta["join_token"] = invite.get("invite_token")


def merge_group_roster_sync(meta: dict, roster: dict) -> None:
    if roster.get("format") != GROUP_ROSTER_FORMAT or roster.get("version") != 1:
        raise ValueError("unsupported group roster sync")
    owner_b32 = str(roster.get("owner_b32") or "").lower()
    if not is_valid_b32_address(owner_b32):
        raise ValueError("group roster owner address is invalid")

    if meta.get("owner_b32") and str(meta["owner_b32"]).lower() != owner_b32:
        raise ValueError("group roster owner does not match stored group owner")

    existing_pubkey = meta.get("roster_signing_pubkey")
    if existing_pubkey and existing_pubkey != roster.get("roster_signing_pubkey"):
        raise ValueError("group roster signing key does not match stored group key")

    members = [normalize_member(member) for member in roster.get("members") or [] if is_valid_b32_address(str(member.get("b32") or ""))]
    verify_group_roster_signature(
        roster.get("group_name") or "",
        owner_b32,
        int(roster.get("roster_version") or 1),
        members,
        roster.get("roster_signing_pubkey") or "",
        roster.get("roster_signature") or "",
    )

    if int(roster.get("roster_version") or 1) >= int(meta.get("roster_version") or 1):
        meta["name"] = roster.get("group_name") or meta.get("name") or "group"
        meta["owner_b32"] = owner_b32
        meta["id"] = owner_b32
        meta["members"] = []
        for member in members:
            merge_group_member(meta, member)
        meta["roster_version"] = int(roster.get("roster_version") or 1)
        meta["roster_signing_pubkey"] = roster.get("roster_signing_pubkey")
        meta["roster_signature"] = roster.get("roster_signature")


@dataclass
class GroupStore:
    base_dir: str
    groups_dir: str = field(init=False)

    def __post_init__(self):
        self.groups_dir = os.path.join(self.base_dir, "groups")
        os.makedirs(self.groups_dir, mode=0o700, exist_ok=True)

    def path_for_key(self, key: str) -> str:
        return os.path.join(self.groups_dir, safe_group_key(key), "group.json")

    def dir_for_key(self, key: str) -> str:
        return os.path.dirname(self.path_for_key(key))

    def runtime_lock_path(self, key: str) -> str:
        return os.path.join(self.dir_for_key(key), "runtime.lock")

    def save(self, meta: dict) -> str:
        key = group_storage_key(meta)
        path = self.path_for_key(key)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return key

    def delete_key(self, key: str) -> None:
        path = os.path.dirname(self.path_for_key(key))
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def load(self, key: str) -> dict:
        with open(self.path_for_key(key), "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        if not meta.get("id"):
            meta["id"] = key
        return meta

    def list_groups(self) -> list[dict]:
        groups = []
        if not os.path.isdir(self.groups_dir):
            return groups
        for name in sorted(os.listdir(self.groups_dir)):
            path = os.path.join(self.groups_dir, name, "group.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    groups.append(json.load(handle))
            except Exception:
                continue
        groups.sort(key=lambda item: (str(item.get("name") or "").lower(), group_storage_key(item)))
        return groups

    def find(self, selector: str) -> Optional[dict]:
        selector_l = selector.strip().lower()
        if not selector_l:
            return None
        for meta in self.list_groups():
            if group_storage_key(meta).lower() == selector_l:
                return meta
            if str(meta.get("name") or "").lower() == selector_l:
                return meta
            if group_storage_key(meta).lower().startswith(selector_l):
                return meta
        return None


class GroupRuntimeLock:
    def __init__(self, store: GroupStore, key: str):
        self.path = store.runtime_lock_path(key)
        self.fd = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.fd.close()
            self.fd = None
            raise RuntimeError("group is already open in another instance") from exc
        self.fd.seek(0)
        self.fd.truncate()
        self.fd.write(str(os.getpid()))
        self.fd.flush()

    def release(self) -> None:
        if not self.fd:
            return
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self.fd.close()
            finally:
                self.fd = None


def group_runtime_is_locked(store: GroupStore, key: str) -> bool:
    lock = GroupRuntimeLock(store, key)
    try:
        lock.acquire()
        return False
    except RuntimeError:
        return True
    finally:
        lock.release()
