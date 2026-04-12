import os
import sys
import time
import signal
import getpass
import asyncio
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.spinner import Spinner

from e2e import E2E
from sam_client import SAMClient
from vault import fs_decrypt, fs_encrypt, fs_runtime_enter, fs_runtime_leave, fs_verify_passphrase


BASE_DIR = os.path.abspath(os.path.join(os.path.expanduser("~"), ".termchat-i2p"))
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
DEFAULT_INTERVAL = 60.0
WINDOW_CAP = 256

RUN_TOKEN = int(time.time())
FS_PASSPHRASE = None
FS_INSTANCE_COUNT = 0
SHUTDOWN_EVENT = asyncio.Event()
MAIN_TASK = None


@dataclass
class ProfileWatchState:
    profile: str
    key_file: str
    locked_peer: Optional[str]
    locked_peer_dest_b64: Optional[str]
    servers: list[str]
    offline_shared_secret: Optional[bytes]
    drop_recv_base: int
    drop_window: int
    consumed_drop_recv: set[int]
    my_b32: Optional[str]


@dataclass
class ProfileWatchResult:
    profile: str
    peer: str
    status: str
    hits: int
    detail: str


class WatcherDeadDropClient:
    def __init__(self, session_id: str, drops: list[str], sam_host: str = "127.0.0.1", sam_port: int = 7656):
        self.session_id = session_id
        self.drops = drops
        self.sam_host = sam_host
        self.sam_port = sam_port
        self.ctrl_reader = None
        self.ctrl_writer = None
        self.connect_timeout = 8.0
        self.io_timeout = 8.0

    async def start(self):
        self.ctrl_reader, self.ctrl_writer = await asyncio.open_connection(self.sam_host, self.sam_port)
        self.ctrl_writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await self.ctrl_writer.drain()
        await self.ctrl_reader.readline()

        cmd = (
            f"SESSION CREATE STYLE=STREAM "
            f"ID={self.session_id} "
            f"DESTINATION=TRANSIENT "
            f"SIGNATURE_TYPE=7 "
            f"OPTION inbound.length=2 outbound.length=2 "
            f"inbound.quantity=2 outbound.quantity=2\n"
        )
        self.ctrl_writer.write(cmd.encode())
        await self.ctrl_writer.drain()
        resp = await self.ctrl_reader.readline()
        resp_str = resp.decode().strip()
        if "RESULT=OK" not in resp_str:
            raise RuntimeError(f"watcher GET session failed: {resp_str}")

    async def _connect(self, destination: str):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.sam_host, self.sam_port),
            timeout=self.connect_timeout,
        )

        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)

        cmd = f"STREAM CONNECT ID={self.session_id} DESTINATION={destination}\n"
        writer.write(cmd.encode())
        await writer.drain()

        resp = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
        resp_str = resp.decode().strip()
        if "RESULT=OK" not in resp_str:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"watcher CONNECT failed: {resp_str}")

        return reader, writer
    
    
    
    async def naming_lookup(self, name: str) -> str:
        if not self.ctrl_writer or not self.ctrl_reader:
            raise RuntimeError("watcher control session is not started")

        cmd = f"NAMING LOOKUP NAME={name}\n"
        self.ctrl_writer.write(cmd.encode())
        await self.ctrl_writer.drain()

        resp = await self.ctrl_reader.readline()
        resp_str = resp.decode().strip()

        parts = resp_str.split()

        result = None
        value = None

        for part in parts:
            if part.startswith("RESULT="):
                result = part.split("=", 1)[1]
            elif part.startswith("VALUE="):
                value = part.split("=", 1)[1]

        if result != "OK" or not value:
            raise RuntimeError(f"watcher NAMING LOOKUP failed: {resp_str}")

        return value
    
    

    async def _get_one(self, drop: str, key: str):
        try:
            reader, writer = await self._connect(drop)
            writer.write(f"GET {key}\n".encode())
            await asyncio.wait_for(writer.drain(), timeout=self.io_timeout)

            header = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
            data = None
            if header.startswith(b"OK"):
                size = int(header.split()[1])
                data = await asyncio.wait_for(reader.readexactly(size), timeout=self.io_timeout)

            writer.close()
            await writer.wait_closed()
            return (drop, data)
        except Exception:
            return (drop, None)

    async def get(self, key: str):
        tasks = [asyncio.create_task(self._get_one(drop, key)) for drop in self.drops]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return [(drop, data) for drop, data in results if data is not None]

    async def close(self):
        try:
            if self.ctrl_writer:
                self.ctrl_writer.close()
                await self.ctrl_writer.wait_closed()
        except Exception:
            pass
    


console = Console()


def prepare_filesystem():
    global FS_PASSPHRASE, FS_INSTANCE_COUNT

    vault_path = BASE_DIR + ".vault"
    
    console.print("[cyan][WATCHER][/]: Starting deaddrop watcher")
    console.print(f"[cyan][WATCHER][/]: Base directory: {BASE_DIR}")

    if os.path.exists(vault_path):
        console.print("[cyan][WATCHER][/]: Encrypted filesystem vault detected")
        FS_PASSPHRASE = getpass.getpass("Enter filesystem passphrase: ")

        try:
            if os.path.exists(BASE_DIR):
                console.print("[cyan][WATCHER][/]: Plaintext filesystem already available")
                if not fs_verify_passphrase(BASE_DIR, FS_PASSPHRASE):
                    console.print("[red][FS ERROR] Wrong filesystem passphrase.[/]")
                    sys.exit(1)
            else:
                console.print("[cyan][WATCHER][/]: Decrypting filesystem vault")
                fs_decrypt(BASE_DIR, FS_PASSPHRASE)
                console.print("[green][WATCHER][/]: Filesystem decrypted")
                
        except Exception as e:
            msg = str(e).lower()
            if "wrong passphrase" in msg or "corrupted filesystem vault" in msg:
                console.print("[red][FS ERROR] Wrong filesystem passphrase.[/]")
            else:
                console.print(f"[red][FS ERROR] Failed to unlock filesystem storage: {e}[/]")
            sys.exit(1)

    FS_INSTANCE_COUNT = fs_runtime_enter(BASE_DIR)
    console.print(f"[cyan][WATCHER][/]: Runtime instances active: {FS_INSTANCE_COUNT}")


def finalize_filesystem():
    
    global FS_INSTANCE_COUNT
    
    console.print("[cyan][WATCHER][/]: Shutting down deaddrop watcher")

    try:
        remaining = fs_runtime_leave(BASE_DIR)
        FS_INSTANCE_COUNT = remaining
        console.print(f"[cyan][WATCHER][/]: Runtime instances remaining: {remaining}")
        
    except Exception:
        remaining = None

    try:
        if remaining == 0 and FS_PASSPHRASE and os.path.exists(BASE_DIR):
            console.print("[cyan][WATCHER][/]: Last instance exited, re-encrypting filesystem")
            fs_encrypt(BASE_DIR, FS_PASSPHRASE)
            console.print("[green][WATCHER][/]: Filesystem encrypted")
        elif remaining is not None and remaining > 0:
            console.print("[cyan][WATCHER][/]: Leaving filesystem plaintext for other running instance(s)")
            
    except Exception as e:
        console.print(f"[red][FS ERROR] Failed to re-encrypt filesystem storage: {e}[/]")


def install_signal_handlers():
    
    def _trigger_shutdown(*_args):
        
        global MAIN_TASK
        
        try:
            console.print("[cyan][WATCHER][/]: Shutdown signal received")
            SHUTDOWN_EVENT.set()
            if MAIN_TASK is not None:
                MAIN_TASK.cancel()
            
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _trigger_shutdown)
        except Exception:
            pass



def print_help():
    console.print("[bold]DeadDrop Watcher[/]")
    console.print("")
    console.print("Usage:")
    console.print("  python ddwatcher.py [--once] [--interval <seconds>] [--help]")
    console.print("")
    console.print("Options:")
    console.print("  --once               Do one scan and exit")
    console.print("  --interval <seconds> Set scan interval for continuous mode (minimum: 10)")
    console.print("  --help               Show this help and exit")



def parse_args():
    interval = DEFAULT_INTERVAL
    once = False

    raw = sys.argv[1:]
    i = 0
    while i < len(raw):
        arg = raw[i]

        if arg == "--help":
            print_help()
            sys.exit(0)


        if arg == "--once":
            once = True
            i += 1
            continue

        if arg == "--interval" and i + 1 < len(raw):
            try:
                interval = max(5.0, float(raw[i + 1]))
            except ValueError:
                console.print("[red]Invalid --interval value[/]")
                sys.exit(1)
            i += 2
            continue

        console.print(f"[red]Unknown argument:[/] {arg}")
        console.print("Usage: python offline_watcher.py [--once] [--interval <seconds>] [--help]")
        sys.exit(1)

    return once, interval


def load_key_file(profile_dir: str, profile: str):
    key_file = os.path.join(profile_dir, f"{profile}.dat")
    if not os.path.exists(key_file):
        return key_file, None, None

    try:
        with open(key_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        locked_peer = lines[1] if len(lines) > 1 else None
        locked_peer_dest_b64 = lines[2] if len(lines) > 2 else None
        return key_file, locked_peer, locked_peer_dest_b64
    except Exception:
        return key_file, None, None


def load_servers(profile_dir: str) -> list[str]:
    path = os.path.join(profile_dir, "deaddrop_servers.txt")
    if not os.path.exists(path):
        return []

    servers: list[str] = []
    seen = set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip().lower()
                if not s or s in seen:
                    continue
                seen.add(s)
                servers.append(s)
    except Exception:
        return []

    return servers


def load_offline_state(profile_dir: str, locked_peer: str):
    peer = locked_peer.replace(".b32.i2p", "").strip().lower()
    path = os.path.join(profile_dir, f"offline_{peer}.state")
    if not os.path.exists(path):
        return None, 0, 0, set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        data = {}
        for line in lines:
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()

        shared = None
        if "offline_shared_secret" in data:
            try:
                shared = bytes.fromhex(data["offline_shared_secret"])
            except Exception:
                shared = None

        drop_recv_base = int(data.get("drop_recv_base", 0))
        drop_window = int(data.get("drop_window", 8))
        consumed = set(
            int(x) for x in data.get("consumed_drop_recv", "").split(",") if x.strip()
        )

        return shared, drop_recv_base, drop_window, consumed
    except Exception:
        return None, 0, 0, set()


def load_profiles() -> list[ProfileWatchState]:
    if not os.path.exists(BASE_DIR):
        raise RuntimeError(f"Base directory is not available: {BASE_DIR}")

    if not os.path.exists(PROFILES_DIR):
        return []

    profiles: list[ProfileWatchState] = []

    for name in sorted(os.listdir(PROFILES_DIR)):
        profile_dir = os.path.join(PROFILES_DIR, name)
        if not os.path.isdir(profile_dir):
            continue
        if name == "default":
            continue

        key_file, locked_peer, locked_peer_dest_b64 = load_key_file(profile_dir, name)
        if not locked_peer:
            continue

        servers = load_servers(profile_dir)
        shared, drop_recv_base, drop_window, consumed = load_offline_state(profile_dir, locked_peer)

        profiles.append(
            ProfileWatchState(
                profile=name,
                key_file=key_file,
                locked_peer=locked_peer,
                locked_peer_dest_b64=locked_peer_dest_b64,
                servers=servers,
                offline_shared_secret=shared,
                drop_recv_base=drop_recv_base,
                drop_window=drop_window,
                consumed_drop_recv=consumed,
                my_b32=None,
            )
        )

    return profiles




def refresh_profiles_from_disk(existing_profiles: list[ProfileWatchState]) -> list[ProfileWatchState]:
    fresh_profiles = load_profiles()
    fresh_map = {p.profile: p for p in fresh_profiles}

    refreshed = []

    for old in existing_profiles:
        fresh = fresh_map.get(old.profile)

        if fresh is None:
            # Profile removed from disk (keep old one for watcher midrun(!)
            refreshed.append(old)
            continue

        # Keep resolved my_b32 from startup (refresh everything else from disk)
        fresh.my_b32 = old.my_b32
        refreshed.append(fresh)

    return refreshed




async def resolve_profile_my_b32(profile: str, key_file: str) -> tuple[Optional[str], Optional[str]]:
    if not os.path.exists(key_file):
        return None, "identity file missing"

    try:
        with open(key_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        if not lines:
            return None, "identity file empty"

        my_dest_b64 = lines[0]

        sam = SAMClient()
        await sam.connect()

        temp_session_id = f"watch_id_{profile}_{int(time.time() * 1000)}"
        await sam.create_session(
            temp_session_id,
            destination=my_dest_b64,
            options={
                "inbound.length": "2",
                "outbound.length": "2",
                "inbound.quantity": "2",
                "outbound.quantity": "2",
            }
        )

        my_pub_dest_b64 = await sam.naming_lookup("ME")
        my_b32 = sam.destination_to_b32(my_pub_dest_b64)

        await sam.close()
        return my_b32, None

    except Exception as e:
        return None, f"identity resolution failed: {e}"




def derive_recv_window_keys(my_b32: str, peer_b32: str, shared_secret: bytes, recv_base: int, recv_window: int, consumed: set[int]):
    my_b32 = my_b32.replace(".b32.i2p", "").strip().lower()
    peer_b32 = peer_b32.replace(".b32.i2p", "").strip().lower()

    low_id, high_id = sorted([my_b32, peer_b32])

    if my_b32 == low_id:
        recv_label = "HIGH_TO_LOW"
    else:
        recv_label = "LOW_TO_HIGH"

    window = min(max(1, recv_window), WINDOW_CAP)

    keys = []
    for i in range(recv_base, recv_base + window):
        if i in consumed:
            continue

        material = b"|".join([
            shared_secret,
            low_id.encode(),
            high_id.encode(),
            recv_label.encode(),
            str(i).encode(),
        ])
        key = __import__("hashlib").sha256(material).hexdigest()
        keys.append((i, key))

    return keys


async def check_profile(profile_state: ProfileWatchState, dd: WatcherDeadDropClient) -> ProfileWatchResult:
    peer = profile_state.locked_peer or "-"

    if not profile_state.locked_peer:
        return ProfileWatchResult(profile_state.profile, "-", "SKIP", 0, "no locked peer")

    if not profile_state.offline_shared_secret:
        return ProfileWatchResult(profile_state.profile, peer, "SKIP", 0, "offline secret missing")

    if not profile_state.servers:
        return ProfileWatchResult(profile_state.profile, peer, "SKIP", 0, "no deaddrop servers")

    my_b32 = profile_state.my_b32
    if not my_b32:
        return ProfileWatchResult(profile_state.profile, peer, "FAIL", 0, "identity resolution failed")

    try:
        window_keys = derive_recv_window_keys(
            my_b32=my_b32,
            peer_b32=profile_state.locked_peer,
            shared_secret=profile_state.offline_shared_secret,
            recv_base=profile_state.drop_recv_base,
            recv_window=profile_state.drop_window,
            consumed=profile_state.consumed_drop_recv,
        )
    except Exception as e:
        return ProfileWatchResult(profile_state.profile, peer, "FAIL", 0, f"window error: {e}")

    if not window_keys:
        return ProfileWatchResult(profile_state.profile, peer, "IDLE", 0, "empty receive window")

    hits = 0
    checked = 0
    e2e = E2E(pq_enabled=False)

    try:
        blob_key = e2e.derive_offline_blob_key(
            profile_state.offline_shared_secret,
            my_b32.replace(".b32.i2p", "").strip().lower(),
            profile_state.locked_peer.replace(".b32.i2p", "").strip().lower(),
        )

        for _, key in window_keys:
            checked += 1
            results = await dd.get(key)
            if not results:
                continue

            valid_here = False
            for _, data in results:
                if not data:
                    continue
                try:
                    e2e.decrypt_offline_blob(data, blob_key)
                    valid_here = True
                    break
                except Exception:
                    continue

            if valid_here:
                hits += 1

        status = "HIT" if hits > 0 else "CLEAR"
        detail = f"window={checked} servers={len(profile_state.servers)}"
        return ProfileWatchResult(profile_state.profile, peer, status, hits, detail)

    except Exception as e:
        return ProfileWatchResult(profile_state.profile, peer, "FAIL", 0, str(e))



def render_placeholder_table(
    profiles: list[ProfileWatchState],
    scan_no: int,
    previous_results: Optional[list[ProfileWatchResult]] = None,
    seconds_to_next_scan: Optional[int] = None,
) -> Table:
    
    if seconds_to_next_scan is None:
        title = f"[bold]DeadDrop Watcher[/]  |  Scan {scan_no}"
    else:
        title = f"[bold]DeadDrop Watcher[/]  |  Scan {scan_no}  |  Scan interval: {seconds_to_next_scan}s"

    table = Table(title=title)
    
    table.add_column("Profile", justify="left")
    table.add_column("Locked Peer", justify="left")
    table.add_column("Status", justify="center")
    table.add_column("Hits", justify="right")
    table.add_column("Details", justify="left")

    previous_map = {}
    if previous_results:
        previous_map = {row.profile: row for row in previous_results}

    for profile in profiles:
        prev = previous_map.get(profile.profile)

        hits_text = str(prev.hits) if prev else "..."
        detail_text = prev.detail if prev else f"window={profile.drop_window} servers={len(profile.servers)}"

        table.add_row(
            profile.profile,
            profile.locked_peer or "-",
            Spinner("dots", text="SCANNING"),
            hits_text,
            detail_text,
        )

    if not profiles:
        table.add_row("-", "-", "[grey62] EMPTY [/ ]".replace(" [/ ]", " [/]"), "0", "no locked persistent profiles")

    return table




def render_table(
    results: list[ProfileWatchResult],
    scan_no: int,
    seconds_to_next_scan: Optional[int] = None,
) -> Table:
    
    if seconds_to_next_scan is None:
        title = f"[bold]DeadDrop Watcher[/]  |  Scan {scan_no}"
    else:
        title = f"[bold]DeadDrop Watcher[/]  |  Scan {scan_no}  |  Next scan in {seconds_to_next_scan}s"

    table = Table(title=title)
    
    table.add_column("Profile", justify="left")
    table.add_column("Locked Peer", justify="left")
    table.add_column("Status", justify="center")
    table.add_column("Hits", justify="right")
    table.add_column("Details", justify="left")

    for row in results:
        if row.status == "HIT":
            status = "[yellow] MESSAGES WAITING [/ ]".replace(" [/ ]", " [/]")
        elif row.status == "CLEAR":
            status = "[green] CLEAR [/ ]".replace(" [/ ]", " [/]")
        elif row.status == "SKIP":
            status = "[grey62] SKIP [/ ]".replace(" [/ ]", " [/]")
        else:
            status = "[red] FAIL [/ ]".replace(" [/ ]", " [/]")

        table.add_row(
            row.profile,
            row.peer,
            status,
            str(row.hits),
            row.detail,
        )

    if not results:
        table.add_row("-", "-", "[grey62] EMPTY [/ ]".replace(" [/ ]", " [/]") , "0", "no locked persistent profiles")

    return table


async def run_once(scan_no: int, profiles: list[ProfileWatchState], clients: dict[str, WatcherDeadDropClient]) -> Table:
    results = []
    for profile in profiles:
        client = clients.get(profile.profile)
        if client is None:
            results.append(ProfileWatchResult(profile.profile, profile.locked_peer or "-", "FAIL", 0, "watcher client missing"))
            continue
        results.append(await check_profile(profile, client))
    return render_table(results, scan_no)


async def build_clients(profiles: list[ProfileWatchState]) -> dict[str, WatcherDeadDropClient]:
    clients: dict[str, WatcherDeadDropClient] = {}

    for profile in profiles:
        if not profile.servers:
            continue

        client = WatcherDeadDropClient(
            session_id=f"watch_{profile.profile}_{RUN_TOKEN}",
            drops=list(profile.servers),
        )
        
        await client.start()

        my_b32, err = await resolve_profile_my_b32(profile.profile, profile.key_file)
        if err or not my_b32:
            profile.my_b32 = None
        else:
            profile.my_b32 = my_b32

        clients[profile.profile] = client

    return clients




async def close_clients(clients: dict[str, WatcherDeadDropClient]):
    for client in clients.values():
        try:
            await client.close()
        except Exception:
            pass



async def main():
    
    global MAIN_TASK
    
    MAIN_TASK = asyncio.current_task()
    
    once, interval = parse_args()
    
    prepare_filesystem()
    install_signal_handlers()

    if not os.path.exists(BASE_DIR):
        console.print("[red]Filesystem is not unlocked or base directory does not exist.[/]")
        sys.exit(1)

    

    profiles = load_profiles()
    console.print(f"[cyan][WATCHER][/]: Loaded {len(profiles)} locked persistent profile(s)")
    clients = await build_clients(profiles)
    console.print(f"[cyan][WATCHER][/]: Started {len(clients)} watcher GET session(s)")

    try:
        if once:
            with Live(render_placeholder_table(profiles, 1, None, None), console=console, refresh_per_second=8, transient=False) as live:
                table = await run_once(1, profiles, clients)
                live.update(table)
            return

        scan_no = 0
        previous_results = None

        with Live(console=console, refresh_per_second=8, transient=False) as live:
            while not SHUTDOWN_EVENT.is_set():
                scan_no += 1

                profiles = refresh_profiles_from_disk(profiles)

                live.update(render_placeholder_table(profiles, scan_no, previous_results, int(interval)))

                results = []
                for profile in profiles:
                    client = clients.get(profile.profile)
                    if client is None:
                        results.append(ProfileWatchResult(profile.profile, profile.locked_peer or "-", "FAIL", 0, "watcher client missing"))
                        continue
                    results.append(await check_profile(profile, client))

                previous_results = results

                remaining = int(interval)
                live.update(render_table(results, scan_no, remaining))

                while remaining > 0 and not SHUTDOWN_EVENT.is_set():
                    try:
                        await asyncio.wait_for(SHUTDOWN_EVENT.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        remaining -= 1
                        live.update(render_table(results, scan_no, remaining))
                
    finally:
        try:
            await close_clients(clients)
        finally:
            finalize_filesystem()
            MAIN_TASK = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
