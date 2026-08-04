APP_NAME = "Termchat-I2P"
APP_VERSION = "1.0.0-rc2"

import sys, os
import shutil
import stat
import getpass
import tarfile
from io import BytesIO
import tempfile

import asyncio
from textual.app import App, ComposeResult
from textual import events
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea
from textual.containers import Container, ScrollableContainer
from textual.reactive import reactive
from datetime import datetime, timezone
import re
from rich.markup import escape
from rich.panel import Panel
from rich import box
from rich.align import Align
from rich.table import Table
from rich.text import Text
import time
import pyperclip
import base64
from deaddrop_screens import DeadDropManagerScreen
from renderer import render_braille_color, render_bw
from PIL import Image

import struct
import random
import hashlib
import json

from deaddrop import DeadDropClient
from e2e import E2E
from file_picker import FilePickerScreen
from group_ops import (
    GROUP_CONTROL_JOIN_PROOF,
    GROUP_CONTROL_RENAME_REQUEST,
    GroupRuntimeLock,
    GroupStore,
    apply_group_member_rename,
    build_group_control,
    compact_json_bytes,
    decode_group_invite_string,
    group_is_admin,
    group_runtime_is_locked,
    group_self_display_name,
    group_storage_key,
    issue_group_invite,
    is_valid_b32_address,
    make_group_meta,
    merge_group_invite,
    merge_group_roster_sync,
    normalize_member,
    redeem_group_invite_token,
    roster_sync_from_meta,
    sign_group_roster_if_admin,
    validate_group_display_name,
)
from group_screens import ActiveGroupScreen, GroupManagerScreen
from help_screen import HELP_LINES, HelpScreen
from logs_screen import LogsScreen
from profiles_screen import ProfilesScreen
from sam_client import SAMClient
from sam_runtime import SamRuntimeClosed, SamSessionManager

from vault import fs_decrypt, fs_encrypt, fs_runtime_enter, fs_runtime_leave, fs_verify_passphrase



MAGIC = b"\x89I2P"
PROTOCOL_VERSION = 3

REPLY_BEGIN_MARKER = "[ICEDCOMM-REPLY-v1]"
REPLY_QUOTE_MARKER = "[ICEDCOMM-QUOTE]"
REPLY_END_MARKER = "[/ICEDCOMM-REPLY]"

# SECURITY LIMITS
MAX_FRAME_SIZE = 256 * 1024      # 256 KB max protocol frame
MAX_FILE_SIZE = 50 * 1024 * 1024 # 50 MB max file
MAX_IMAGE_LINES = 2000           # prevents huge ASCII images
MAX_FILENAME = 128
IMAGE_RENDER_WIDTH = 60
IMAGE_TRANSFER_MAX_DIMENSION = 1280
IMAGE_TRANSFER_JPEG_QUALITY = 82
GROUP_IMAGE_TRANSFER_MAX_BYTES = 2 * 1024 * 1024

Image.MAX_IMAGE_PIXELS = 20_000_000

MAX_ACTIVE_DEADDROP_REPLICAS = 3 # Offline replication const

DD_STATS_EMA_ALPHA = 0.30
DD_FAILURE_PENALTY = 2500.0
DD_UNKNOWN_SERVER_SCORE = -1e18
DD_STATS_SAVE_INTERVAL = 15.0

HEARTBEAT_PING_INTERVAL = 10.0
HEARTBEAT_TIMEOUT = 35.0
HEARTBEAT_PING_PREFIX = "__SIGNAL__:PING:"
HEARTBEAT_PONG_PREFIX = "__SIGNAL__:PONG:"
OFFLINE_SECRET_REQUEST_SIGNAL = "__SIGNAL__:OFFLINE_SECRET_REQUEST"
GROUP_RECONNECT_INTERVAL = 5.0
GROUP_HANDSHAKE_TIMEOUT = 45.0

BASE_DIR = os.path.join(os.path.expanduser("~"), ".termchat-i2p")
BASE_DIR = os.path.abspath(BASE_DIR)

DIR_MODE = 0o700
FILE_MODE = 0o600

try:
    os.umask(0o077)
except:
    pass


def secure_makedirs(path: str):
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except:
        pass


def secure_write_text(path: str, text: str, mode: str = "w"):
    with open(path, mode) as f:
        f.write(text)
    try:
        os.chmod(path, FILE_MODE)
    except:
        pass


def secure_append_text(path: str, text: str):
    with open(path, "a") as f:
        f.write(text)
    try:
        os.chmod(path, FILE_MODE)
    except:
        pass



def secure_write_text_atomic(path: str, text: str):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=DIR_MODE, exist_ok=True)
        try:
            os.chmod(parent, DIR_MODE)
        except:
            pass

    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir=parent if parent else None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        try:
            os.chmod(tmp_path, FILE_MODE)
        except:
            pass

        os.replace(tmp_path, path)

        try:
            os.chmod(path, FILE_MODE)
        except:
            pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass




def secure_delete_profile(profile_name: str):
    profile_dir = os.path.join(BASE_DIR, "profiles", os.path.basename(profile_name))
    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)
        print(f"[OK] Deleted profile: {profile_name}")
    else:
        print(f"[INFO] Profile does not exist: {profile_name}")
        


def list_persistent_profile_rows() -> list[dict]:
    profiles_dir = os.path.join(BASE_DIR, "profiles")
    rows = []
    if not os.path.isdir(profiles_dir):
        return rows

    for name in sorted(os.listdir(profiles_dir), key=str.lower):
        if name == "default" or name.startswith("."):
            continue
        profile_dir = os.path.join(profiles_dir, name)
        if not os.path.isdir(profile_dir):
            continue

        peer_b32 = ""
        key_file = os.path.join(profile_dir, f"{name}.dat")
        if os.path.isfile(key_file):
            try:
                with open(key_file, "r", encoding="utf-8") as handle:
                    lines = [line.strip() for line in handle.readlines() if line.strip()]
                if len(lines) > 1:
                    peer_b32 = lines[1]
            except Exception:
                peer_b32 = ""

        rows.append({
            "index": len(rows) + 1,
            "profile": name,
            "state": "LOCKED" if peer_b32 else "UNLOCKED",
            "peer_b32": peer_b32,
        })

    return rows



def confirm_action(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")

        


def secure_wipe_all():
    try:
        if os.path.exists(BASE_DIR):
            shutil.rmtree(BASE_DIR, ignore_errors=True)

        vault_path = BASE_DIR + ".vault"
        meta_path = BASE_DIR + ".vault.meta"
        vault_lock_path = BASE_DIR + ".vault.lock"

        if os.path.exists(vault_path):
            os.remove(vault_path)

        if os.path.exists(meta_path):
            os.remove(meta_path)

        if os.path.exists(vault_lock_path):
            os.remove(vault_lock_path)

        print("[OK] Removed all application data.")
    except Exception as e:
        print(f"[FS ERROR] Failed to wipe all application data: {e}")
        sys.exit(1)



def export_encrypted_vault(export_path: str):
    vault_path = BASE_DIR + ".vault"
    meta_path = BASE_DIR + ".vault.meta"

    if os.path.exists(BASE_DIR):
        print("[FS ERROR] Cannot export while plaintext filesystem is present.")
        print("Close all instances and make sure the filesystem is encrypted first.")
        sys.exit(1)

    if not os.path.exists(vault_path) or not os.path.exists(meta_path):
        print("[FS ERROR] Encrypted vault files not found.")
        sys.exit(1)

    export_path = os.path.abspath(export_path)

    try:
        parent = os.path.dirname(export_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with tarfile.open(export_path, "w:gz") as tar:
            tar.add(vault_path, arcname=os.path.basename(vault_path))
            tar.add(meta_path, arcname=os.path.basename(meta_path))

        print(f"[OK] Exported encrypted vault to: {export_path}")
    except Exception as e:
        print(f"[FS ERROR] Failed to export encrypted vault: {e}")
        sys.exit(1)


def import_encrypted_vault(import_path: str):
    vault_path = BASE_DIR + ".vault"
    meta_path = BASE_DIR + ".vault.meta"

    import_path = os.path.abspath(import_path)

    if not os.path.exists(import_path):
        print(f"[FS ERROR] Import file not found: {import_path}")
        sys.exit(1)

    if os.path.exists(BASE_DIR):
        print("[FS ERROR] Plaintext filesystem already exists.")
        print("Remove or encrypt existing filesystem before import.")
        sys.exit(1)

    if os.path.exists(vault_path) or os.path.exists(meta_path):
        print("[FS ERROR] Existing encrypted vault already exists.")
        print("Use --wipe-all first if you really want to replace it.")
        sys.exit(1)

    try:
        with tarfile.open(import_path, "r:gz") as tar:
            names = set(tar.getnames())

            expected_vault = os.path.basename(vault_path)
            expected_meta = os.path.basename(meta_path)

            if expected_vault not in names or expected_meta not in names:
                print("[FS ERROR] Import archive does not contain required vault files.")
                sys.exit(1)

            tar.extractall(path=os.path.dirname(BASE_DIR))

        print(f"[OK] Imported encrypted vault from: {import_path}")
    except Exception as e:
        print(f"[FS ERROR] Failed to import encrypted vault: {e}")
        sys.exit(1)


        

def ensure_deaddrop_bootstrap_file():
    if os.path.exists(DD_BOOTSTRAP_FILE):
        return

    content = (
        "62afc5yf2lcthx44okvavvmvgb55cee3weqeqhuapcclz6evwyrq.b32.i2p\n"
        "x75crc4lkcd3xcfrj5sox662mujngzrtmvmejaixutdozg35fgvq.b32.i2p\n"
        "xxbgj3dlw7fvwz3emqnvyzxrdj3vqd3fcdw6rutmvzoxidyhp7bq.b32.i2p\n"
    )

    secure_write_text_atomic(DD_BOOTSTRAP_FILE, content)



def print_help():
    print(f"{APP_NAME} {APP_VERSION}")
    print("")
    print("Usage:")
    print("  termchat-i2p [profile_name] [--pq]")
    print("  termchat-i2p --groups")
    print("  termchat-i2p --group <group_name_or_key>")
    print("  termchat-i2p --help")
    print("  termchat-i2p --wipe-all")
    print("  termchat-i2p --reset <profile_name>")
    print("  termchat-i2p --delete <profile_name>")
    print("  termchat-i2p --export <file>")
    print("  termchat-i2p --import <file>")
    print("")
    print("Modes:")
    print("  no profile name      Start in TRANSIENT mode")
    print("  profile_name         Start/Use a PERSISTENT profile")
    print("  --groups            Open group workspace")
    print("  --group <group>     Open one dedicated group chat")
    print("")
    print("Options:")
    print("  --pq                Enable post-quantum hybrid mode")
    print("  --groups            Manage groups and open one group chat")
    print("  --group <group>     Start a dedicated group chat session")
    print("  --help              Show this help and exit")
    print("  --wipe-all          Remove all application data")
    print("  --reset <profile>   Reset one persistent profile")
    print("  --delete <profile>  Delete one persistent profile")
    print("  --export <file>     Export encrypted vault+meta to a single archive")
    print("  --import <file>     Import encrypted vault+meta from an archive")
    print("")
    print("Notes:")
    print("  'default' is a reserved internal transient profile name.")
    print("  Do not pass 'default' explicitly as a profile name.")



def validate_profile_name_or_exit(name: str):
    if name == "default":
        print("[ERROR] 'default' is a reserved internal TRANSIENT profile name.")
        print("Start without a profile name for TRANSIENT mode.")
        sys.exit(1)

    if name.startswith("-"):
        print(f"[ERROR] Unknown option: {name}")
        print("Use --help to see available options.")
        sys.exit(1)



RESET_PROFILE = False
DELETE_PROFILE = False
WIPE_ALL = False
EXPORT_VAULT = False
IMPORT_VAULT = False
PQ_ENABLED = False
APP_MODE = "contact"
GROUP_SELECTOR = None
EXPORT_PATH = None
IMPORT_PATH = None



raw_args = sys.argv[1:]

if "--help" in raw_args:
    print_help()
    sys.exit(0)

if "--pq" in raw_args:
    PQ_ENABLED = True
    raw_args = [a for a in raw_args if a != "--pq"]

if len(raw_args) > 0 and raw_args[0] == "--wipe-all":
    WIPE_ALL = True
    PROFILE_NAME = "default"

elif len(raw_args) == 1 and raw_args[0] == "--groups":
    APP_MODE = "groups"
    PROFILE_NAME = "default"

elif len(raw_args) == 2 and raw_args[0] == "--group":
    APP_MODE = "group"
    GROUP_SELECTOR = raw_args[1].strip()
    if not GROUP_SELECTOR:
        print("[ERROR] --group requires a group name or key.")
        sys.exit(1)
    PROFILE_NAME = "default"

elif len(raw_args) > 0 and raw_args[0] in ("--groups", "--group"):
    print("[ERROR] Invalid group mode arguments.")
    print("Use --help to see available options.")
    sys.exit(1)

elif len(raw_args) > 1 and raw_args[0] == "--reset":
    RESET_PROFILE = True
    PROFILE_NAME = os.path.basename(raw_args[1])
    validate_profile_name_or_exit(PROFILE_NAME)

elif len(raw_args) > 1 and raw_args[0] == "--delete":
    DELETE_PROFILE = True
    PROFILE_NAME = os.path.basename(raw_args[1])
    validate_profile_name_or_exit(PROFILE_NAME)

elif len(raw_args) > 1 and raw_args[0] == "--export":
    EXPORT_VAULT = True
    EXPORT_PATH = raw_args[1]
    PROFILE_NAME = "default"

elif len(raw_args) > 1 and raw_args[0] == "--import":
    IMPORT_VAULT = True
    IMPORT_PATH = raw_args[1]
    PROFILE_NAME = "default"

elif len(raw_args) > 0:
    PROFILE_NAME = os.path.basename(raw_args[0])
    validate_profile_name_or_exit(PROFILE_NAME)

else:
    PROFILE_NAME = "default"
    

PROFILE_DIR = os.path.join(BASE_DIR, "profiles", PROFILE_NAME)

IMAGE_DIR = os.path.join(BASE_DIR, "images")
FILE_DIR = os.path.join(BASE_DIR, "files")
BLOB_DIR = os.path.join(BASE_DIR, "blobs")

DD_BOOTSTRAP_FILE = os.path.join(BASE_DIR, "deaddrop_servers.bootstrap.txt")




if EXPORT_VAULT:
    export_encrypted_vault(EXPORT_PATH)
    sys.exit(0)

if IMPORT_VAULT:
    import_encrypted_vault(IMPORT_PATH)
    sys.exit(0)
    


FS_PASSPHRASE = getpass.getpass("Enter filesystem passphrase: ")

vault_path = BASE_DIR + ".vault"

try:
    if os.path.exists(vault_path):
        if os.path.exists(BASE_DIR):
            if not fs_verify_passphrase(BASE_DIR, FS_PASSPHRASE):
                print("[FS ERROR] Wrong filesystem passphrase.")
                sys.exit(1)
        else:
            fs_decrypt(BASE_DIR, FS_PASSPHRASE)

except Exception as e:
    msg = str(e).lower()

    if "wrong passphrase" in msg or "corrupted filesystem vault" in msg:
        print("[FS ERROR] Wrong filesystem passphrase.")
    else:
        print(f"[FS ERROR] Failed to unlock filesystem storage: {e}")

    sys.exit(1)
    
    
    

if WIPE_ALL:
    if not confirm_action("Wipe ALL application data?"):
        print("[INFO] Cancelled.")
        sys.exit(0)
    secure_wipe_all()
    sys.exit(0)
    
    

    
if DELETE_PROFILE:
    if not confirm_action(f"Delete profile '{PROFILE_NAME}'?"):
        print("[INFO] Cancelled.")
        sys.exit(0)
    secure_delete_profile(PROFILE_NAME)
    sys.exit(0)
    



if RESET_PROFILE and os.path.exists(PROFILE_DIR):
    if not confirm_action(f"Reset profile '{PROFILE_NAME}'?"):
        print("[INFO] Cancelled.")
        sys.exit(0)
    
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)


if APP_MODE == "contact" or RESET_PROFILE or DELETE_PROFILE:
    secure_makedirs(PROFILE_DIR)
secure_makedirs(IMAGE_DIR)
secure_makedirs(FILE_DIR)
secure_makedirs(BLOB_DIR)

ensure_deaddrop_bootstrap_file()

FS_INSTANCE_COUNT = fs_runtime_enter(BASE_DIR)





class CommandInput(Input):
    def _on_paste(self, event: events.Paste) -> None:
        if event.text:
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(event.text)
            else:
                self.replace(event.text, *selection)
        event.stop()


class MessageComposer(TextArea):
    pass


class ChatEntryWidget(Static):
    can_focus = True

    def __init__(self, entry: dict, renderable, *, expand: bool = False):
        super().__init__(renderable, expand=expand)
        self.entry = entry

    def _on_click(self, event: events.Click) -> None:
        self.focus()
        event.stop()


class TermchatI2P(App):
    # This maps "q" or "ctrl+q" to the action "quit"
    BINDINGS = [("q", "quit", "Quit"), ("ctrl+q", "quit", "Quit"), ("c", "copy_focused_bubble", "Copy Bubble"), ("r", "reply_to_focused_bubble", "Reply"), ("alt+s", "send_message_composer", "Send Message"), ("f5", "send_message_composer", "Send Message")]
        
    CSS = """
    #status_bar {
        dock: top;
        height: 3;
        margin: 0 0;
        content-align: center middle;
        background: $surface;
        color: $text;
    }

    #bottom_bar {
        dock: bottom;
        height: 8;
        layout: vertical;
        background: $surface;
    }

    #command_bar {
        height: 1;
        margin: 0 1;
        content-align: center middle;
        background: $surface;
        color: $text;
    }

    #command_input {
        height: 1;
        border: none;
        padding: 0;
        margin: 0 1;
        background: #3a3a3a;
    }

    #message_composer {
        height: 6;
        border: solid #5f5f5f;
        background: $surface;
    }

    #message_composer:focus {
        border: solid cyan;
        background: $boost;
    }

    #message_composer:disabled {
        border: solid #3a3a3a;
        color: #5f5f5f;
    }
    

    #chat_window {
        height: 1fr;
        border: solid white;
        background: $surface;
        overflow-y: auto;
    }

    ChatEntryWidget {
        height: auto;
        padding: 0 1;
    }

    ChatEntryWidget:focus {
        background: #303030;
    }
    """
    
    
    
    

    
    peer_b32 = reactive("Waiting for incoming connections...")
    network_status = reactive("initializing") 

    def __init__(self):
        super().__init__()
        self.app_mode = APP_MODE
        self.group_selector = GROUP_SELECTOR
        self.sam_address = ('127.0.0.1', 7656)
        self.sock = None  # LISTENER
        self.conn = None  # ACTIVE CHAT
        self.live_ready = False
        self.heartbeat_last_rx_ts = 0.0
        self.heartbeat_last_ping_ts = 0.0
        self.heartbeat_task = None
        self.publish_ready = False
        
        
        self.sam_runtime = SamSessionManager(self.sam_address[0], self.sam_address[1])
        self.sam = self.sam_runtime.client
        
        self.stored_peer = None
        self.stored_peer_dest_b64 = None
        self.current_peer_addr = None
        self.current_peer_dest_b64 = None
        
        self.pending_incoming_conn = None
        self.pending_incoming_addr = None
        self.pending_incoming_dest_b64 = None
        self.pending_incoming_task = None
        self.promoting_pending_incoming = False
        self.call_blink_on = True
        
        self.tofu_verified = False
        self.tofu_mismatch = False
        self.pq_active = False
        
        self.profile = PROFILE_NAME
    
        # Generate a unique ID for THIS appinstance
        self.session_id = f"chat_{self.profile}_{int(time.time())}"
        self.proven = False  
        
        #deaddrops
        self.dd_session_id = f"dd_{self.profile}_{int(time.time())}"
        
        
        # File transfer states
        self.incoming_file = None
        self.incoming_filename = None
        self.incoming_expected = 0
        self.incoming_received = 0
        
        self.outgoing_file = None
        self.outgoing_filename = None
        self.outgoing_total = 0
        self.outgoing_sent = 0
        
        self.tx_start_time = None
        self.rx_start_time = None
        
        
        self.incoming_image_name = None
        self.incoming_image_mime = None
        self.incoming_image_expected = 0
        self.incoming_image_received = 0
        self.incoming_image_msg_id = 0
        self.incoming_image_bytes = bytearray()
        
        self.pending_messages = {}
        self.chat_history = []
        self.log_history = []
        self.reply_target = None

        self.group_store = GroupStore(BASE_DIR)
        self.active_group_key = None
        self.active_group = None
        self.group_sam_runtime = None
        self.group_sam = None
        self.group_session_id = None
        self.group_pub_dest_b64 = None
        self.group_accept_task = None
        self.group_reconnect_task = None
        self.group_ready_task = None
        self.group_publish_ready = False
        self.group_peers = {}
        self.group_pending_messages = {}
        self.group_runtime_lock = None
        
        # Command history init
        self.command_history = []
        self.command_history_index = None
        self.command_history_current_buffer = ""
        
        self.pq_enabled = PQ_ENABLED
        
        
        # Better handling of ugly trace messages I hate soo much :)
        try:
            self.e2e = E2E(pq_enabled=self.pq_enabled)
        except Exception as e:
            print(f"[PQ ERROR] {e}")
            sys.exit(1)
        
        
        
        # Deaddrop servers, profile specific, loaded from file
        self.deaddrop_servers = []
        self.deaddrop_stats = {}
        self.deaddrop_stats_dirty = False
        self.deaddrop_stats_last_save_ts = 0.0
        
        
        
        self.deaddrop = DeadDropClient(
            self.dd_session_id,
            self.deaddrop_servers
        )
        
        self.deaddrop.stats_callback = self.record_deaddrop_stat

        
        self.deaddrop_enabled = self.is_persistent_mode()
        self.deaddrop_started = False
        self.deaddrop_poller_started = False
        self.offline_mode = False
        
        self.dd_status = "idle"
        self.dd_status_ts = 0.0
        
        self.seen_drop_msgs = set()
        
        # OFFLINE key window state
        
        self.offline_shared_secret = b"CHANGE_ME_SHARED_OFFLINE_SECRET"

        # One key per message
        self.drop_send_index = 0

        # Receiver window base
        self.drop_recv_base = 0
        self.drop_window = 8

        # Tracks received consumed indexes
        self.consumed_drop_recv = set()


    def compose(self) -> ComposeResult:
               
        yield Static(id="status_bar")
        yield ScrollableContainer(id="chat_window")

        with Container(id="bottom_bar"):
            yield MessageComposer(
                placeholder="Message composer disabled until a peer or group is connected...",
                id="message_composer",
                show_line_numbers=False,
                soft_wrap=True,
                disabled=True,
            )
            yield CommandInput(placeholder="Type command and press Enter...", id="command_input")
            yield Static(id="command_bar")
        


    def watch_network_status(self, _):
        # Refresh panel when network status changes
        self.watch_peer_b32(self.peer_b32)
        
        
        
    def get_command_hints(self) -> str:
        hints = []

        if self.app_mode == "groups" and self.active_group:
            hints = ["/group", "/disconnect", "/logs", "/help"]

        elif self.app_mode == "groups":
            hints = ["/admin", "/logs", "/help"]

        elif self.app_mode == "group":
            hints = ["/group", "/disconnect", "/logs", "/help"]

        elif self.active_group:
            hints = ["/group", "/disconnect", "/logs", "/help"]

        elif self.pending_incoming_conn:
            hints = ["/accept", "/decline", "/logs", "/help"]

        elif self.conn and not self.live_ready:
            hints = ["/disconnect", "/logs", "/help"]

        elif self.conn and self.live_ready:
            hints = ["/disconnect", "/sendfile", "/img", "/img-bw", "/logs", "/help"]

            if self.is_persistent_mode():
                hints.extend(["/dd", "/dd-share"])

            if self.is_persistent_mode() and not self.stored_peer:
                hints.append("/lock")
            elif self.is_persistent_mode() and self.stored_peer:
                hints.append("/unlock")

        elif self.offline_mode:
            hints = ["/online", "/logs", "/help"]

            if self.is_persistent_mode():
                hints.append("/dd")

        else:
            hints = ["/connect", "/logs", "/help"]

            if self.offline_ready():
                hints.insert(1, "/offline")

            if self.is_persistent_mode():
                hints.append("/dd")
                if self.stored_peer:
                    hints.append("/unlock")

        if self.reply_target:
            hints.insert(0, f"replying to {escape(self.reply_target['author'])}")

        return "   ".join(hints)
    
    
    
    
    def update_command_bar(self):
        try:
            hints = self.get_command_hints()
            self.query_one("#command_bar").update(f"[dim]{hints}[/]")
            self.update_message_composer_state()
        except:
            pass
        
        

    
    def watch_peer_b32(self, new_val: str) -> None:
        
        status_map = {
            "initializing": ("[grey62]●[/]", "INITIALIZING", "grey62"),
            "local_ok": ("[yellow]●[/]", "BUILDING TUNNELS", "yellow"),
            "visible": ("[green]●[/]", "VISIBLE / READY", "green")
        }

        if self.app_mode in ("groups", "group"):
            dot, status_text, border_col = status_map.get(self.network_status, status_map["initializing"])
            grid = Table.grid(expand=True)
            grid.add_column(justify="left", ratio=1)
            grid.add_column(justify="center", ratio=1)
            grid.add_column(justify="right", ratio=1)

            if self.active_group:
                group_name = self.active_group.get("name") or "group"
                my_name = group_self_display_name(self.active_group).upper()
                my_group_b32 = self.active_group.get("my_b32") or ""
                clean_group_b32 = my_group_b32.replace(".b32.i2p", "")
                if clean_group_b32:
                    group_b32_display = f"{clean_group_b32[:6]}...{clean_group_b32[-6:]}"
                else:
                    group_b32_display = "----"

                ready_count = sum(
                    1
                    for peer in self.group_peers.values()
                    if peer.get("ready") and peer.get("authorized")
                )
                peer_count = len(self.active_group.get("members") or [])
                display_ready_count = ready_count + 1
                display_peer_count = peer_count + 1
                is_active = ready_count > 0
                border_col = "cyan" if is_active else "yellow"
                title = "ACTIVE SESSION" if is_active else "TUNNELS READY"
                conn_viz = "[bold cyan]o[/] [dim]CONNECTED[/]" if is_active else f"[dim]{dot} [dim]STANDBY[/]"
                left_content = (
                    f"[black on green] [bold]P[/] [/] "
                    f"[black on green] [bold]G[/] [/] "
                    f"[bold]{escape(my_name)}[/] [dim]#{escape(group_name)}[/]"
                )
                right_content = f"[green]{group_b32_display}[/] [white]:[/] [cyan dim]{display_ready_count}/{display_peer_count} active[/]"
            else:
                groups_count = len(self.group_store.list_groups())
                left_content = "[black on green] [bold]G[/] [/] [bold]GROUPS[/]"
                conn_viz = f"[dim]{dot} [dim]STANDBY[/]"
                right_content = f"[cyan]{groups_count}[/] groups"
                title = "TUNNELS READY"

            grid.add_row(left_content, conn_viz, right_content)
            status_panel = Panel(
                grid,
                title=f"[bold {border_col}]{title}[/]",
                border_style=border_col,
                box=box.ROUNDED,
                style="default"
            )

            try:
                self.query_one("#status_bar").update(status_panel)
            except:
                pass

            self.update_command_bar()
            return
    
        dot, _, _ = status_map.get(self.network_status, status_map["initializing"])
    
        
        is_active = "Waiting" not in new_val and "My Addr" not in new_val
        is_proven = getattr(self, 'proven', False)
        is_persistent = self.profile != "default"
    
        # Border / Title logic 
        if is_proven:
            border_col, title = "green", "VERIFIED SESSION"
        elif is_active:
            border_col, title = "cyan", "ACTIVE SESSION"
        else:
            border_col, title = "yellow", "TUNNELS READY"

        # Grid Layout
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)   # Identity
        grid.add_column(justify="center", ratio=1) # Connection
        grid.add_column(justify="right", ratio=1)  # Peer

        
        if is_persistent:
            mode_tag = "P"
            tag_bg = "green" 
        else:
            mode_tag = "T"
            tag_bg = "grey62" 

        
        
        
        lock_tag = " [black on green] LOCK [/]" if self.stored_peer else " [black on red] UNLOCK [/]"
        
        if self.offline_mode:
            tofu_tag = ""
        elif self.tofu_mismatch:
            tofu_tag = " [black on red] TOFU [/]"
        elif self.tofu_verified:
            tofu_tag = " [black on green] TOFU [/]"
        else:
            tofu_tag = ""
        
        pq_tag = " [black on magenta] PQ [/]" if self.pq_active else ""
        
        offline_tag = " [black on yellow] OFF [/]" if self.offline_mode else ""
        
        dd_label = self.get_dd_status_label()
        dd_tag = f" {dd_label}" if self.offline_mode and dd_label else ""
        
        
        if self.pending_incoming_conn:
            call_tag = " [black on cyan] INCOMING CALL [/]" if self.call_blink_on else " [black on grey62] CALL [/]"
        else:
            call_tag = ""
        
        #left_content = f"[black on {tag_bg}] [bold]{mode_tag}[/] [/] [bold]{self.profile.upper()}[/]{lock_tag}{tofu_tag}{offline_tag}{dd_tag}{call_tag}"
        
        left_content = f"[black on {tag_bg}] [bold]{mode_tag}[/] [/] [bold]{self.profile.upper()}[/]{lock_tag}{tofu_tag}{pq_tag}{offline_tag}{dd_tag}{call_tag}"
        
        transfer = self.get_file_transfer_status()
        #dd_label = self.get_dd_status_label()

        if transfer:
            conn_viz = transfer

        elif is_active:
            link_color = "green" if is_proven else "cyan"
            link_symbol = "●" if is_proven else "o"
            conn_viz = f"[bold {link_color}]{link_symbol}[/] [dim]CONNECTED[/]"
                    
        else:
            
            conn_viz = f"[dim]{dot} [dim]STANDBY[/]"

        
        if hasattr(self, 'my_b32'):
            full_addr = self.my_b32
            clean = full_addr.replace(".b32.i2p", "")
            my_b32 = f"{clean[:6]}...{clean[-6:]}"
        else:
            my_b32 = "----"
    
        if is_active:
            
            peer_addr = getattr(self, 'current_peer_addr', None)
            
            if peer_addr:
                
                clean_peer = peer_addr.replace(".b32.i2p", "")
                peer_disp = f"{clean_peer[:6]}..{clean_peer[-6:]}"
            else:
                peer_disp = "??????"
                
                
            right_content = f"[green]{my_b32}[/] [white]:[/] [cyan dim]{peer_disp}[/]"
        else:
            right_content = f"[green]{my_b32}[/] [white]:[/] [cyan dim] ----[/]"


        grid.add_row(left_content, conn_viz, right_content)

        # Panel assembly
        status_panel = Panel(
            grid,
            title=f"[bold {border_col}]{title}[/]",
            border_style=border_col,
            box=box.ROUNDED,
            #padding=(0, 1)
            style="default"
        )

        
    
        try:
            self.query_one("#status_bar").update(status_panel)
        except:
            pass
        
        
        self.update_command_bar()

        
    
    

    def action_copy_focused_bubble(self) -> None:
        entry = self.focused_chat_entry()
        if entry:
            pyperclip.copy(self.chat_entry_copy_text(entry))
            self.post("success", "Copied selected bubble to system clipboard!")
        else:
            self.post("error", "No bubble selected.")


    def copy_my_addr_to_clipboard(self) -> None:
        if not hasattr(self, 'my_b32'):
            self.post("error", "Local b32 address is not ready.")
            return
        pyperclip.copy(self.my_b32)
        self.post("success", "Copied local b32 address to system clipboard!")


    def action_reply_to_focused_bubble(self) -> None:
        entry = self.focused_chat_entry()
        if not entry:
            return

        quote = self.chat_entry_body_text(entry).strip()
        if not quote:
            return

        self.reply_target = {
            "author": self.chat_entry_author(entry),
            "quote": quote,
        }
        self.update_command_bar()
        if self.message_composer_enabled():
            self.query_one("#message_composer", TextArea).focus()
        else:
            self.query_one("#command_input", Input).focus()


    def message_composer_enabled(self) -> bool:
        if self.active_group:
            return any(
                peer.get("ready") and peer.get("authorized") and peer.get("writer")
                for peer in self.group_peers.values()
            )
        if self.offline_ready() and self.offline_mode:
            return True
        return bool(self.conn and self.live_ready)


    def update_message_composer_state(self) -> None:
        try:
            composer = self.query_one("#message_composer", TextArea)
        except:
            return

        enabled = self.message_composer_enabled()
        composer.disabled = not enabled
        if enabled:
            composer.placeholder = "Type multiline message. Alt+S or F5 to send."
        else:
            composer.placeholder = "Message composer disabled until a peer, group, or offline mode is ready..."


    def action_send_message_composer(self) -> None:
        self.run_worker(self.send_message_composer())


    async def send_message_composer(self):
        composer = self.query_one("#message_composer", TextArea)
        if not self.message_composer_enabled():
            self.post("error", "Message composer is disabled until a peer, group, or offline mode is ready.")
            return

        message = composer.text.strip()
        if not message:
            return

        composer.clear()
        if self.active_group:
            message = self.apply_reply_target(message)
            await self.send_group_message(message)
            return

        if self.conn and self.live_ready:
            message = self.apply_reply_target(message)
            await self.send_direct_message(message)
            return

        if self.offline_ready() and self.offline_mode:
            message = self.apply_reply_target(message)
            await self.send_offline_message(message)
            return

        self.post("error", "No active connection.")


    def focused_chat_entry(self):
        focused = getattr(self, "focused", None)
        if isinstance(focused, ChatEntryWidget):
            return focused.entry
        return None


    def focused_chat_widget(self):
        focused = getattr(self, "focused", None)
        return focused if isinstance(focused, ChatEntryWidget) else None


    def chat_entry_author(self, entry) -> str:
        if entry.get("kind") == "group_bubble":
            return "Me" if entry.get("mine") else entry.get("author", "Group")

        type_name = entry.get("type")
        if type_name == "me":
            return "Me"
        if type_name == "peer":
            return "Peer"
        if type_name == "me_offline":
            return "Me-Offline"
        if type_name == "peer_offline":
            return "Peer-Offline"
        return "Message"


    def chat_entry_body_text(self, entry) -> str:
        message = str(entry.get("message") or "")
        reply = self.parse_reply_text(message)
        return reply["body"] if reply else message


    def chat_entry_copy_text(self, entry) -> str:
        message = str(entry.get("message") or entry.get("content") or "")
        reply = self.parse_reply_text(message)
        if not reply:
            return message
        return f"Reply to {reply['author']}:\n{reply['quote']}\n\n{reply['body']}"


    def apply_reply_target(self, message: str) -> str:
        if not self.reply_target or message.startswith("/"):
            return message

        target = self.reply_target
        self.reply_target = None
        self.update_command_bar()
        return (
            f"{REPLY_BEGIN_MARKER}\n"
            f"{target['author']}\n"
            f"{REPLY_QUOTE_MARKER}\n"
            f"{target['quote']}\n"
            f"{REPLY_END_MARKER}\n"
            f"{message}"
        )


    def focus_adjacent_chat_entry(self, direction: int) -> bool:
        current = self.focused_chat_widget()
        if not current:
            return False

        widgets = list(self.chat_log.query(ChatEntryWidget))
        if current not in widgets:
            return False

        next_index = widgets.index(current) + direction
        if next_index < 0 or next_index >= len(widgets):
            return False

        widgets[next_index].focus()
        if hasattr(widgets[next_index], "scroll_visible"):
            widgets[next_index].scroll_visible(animate=False)
        return True



    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        if isinstance(getattr(self, "focused", None), ChatEntryWidget):
            if event.key == "up":
                event.prevent_default()
                event.stop()
                self.focus_adjacent_chat_entry(-1)
                return
            if event.key == "down":
                event.prevent_default()
                event.stop()
                self.focus_adjacent_chat_entry(1)
                return
            if event.key == "enter":
                event.prevent_default()
                event.stop()
                self.query_one("#command_input", Input).focus()
                return

        if event.key not in ("up", "down"):
            return

        #input_widget = self.query_one(Input)
        input_widget = self.query_one("#command_input", Input)

        if not input_widget.has_focus:
            return

        if not self.command_history:
            return

        if event.key == "up":
            event.prevent_default()
            event.stop()

            if self.command_history_index is None:
                self.command_history_current_buffer = input_widget.value
                self.command_history_index = len(self.command_history) - 1
            elif self.command_history_index > 0:
                self.command_history_index -= 1

            input_widget.value = self.command_history[self.command_history_index]
            input_widget.cursor_position = len(input_widget.value)
            return

        if event.key == "down":
            event.prevent_default()
            event.stop()

            if self.command_history_index is None:
                return

            if self.command_history_index < len(self.command_history) - 1:
                self.command_history_index += 1
                input_widget.value = self.command_history[self.command_history_index]
            else:
                self.command_history_index = None
                input_widget.value = self.command_history_current_buffer

            input_widget.cursor_position = len(input_widget.value)
            return


     

    def format_chat_message(self, message: str) -> str:
        normalized = str(message).replace("\r\n", "\n").replace("\r", "\n")
        safe_message = re.sub(r'[\x00-\x09\x0B-\x1F\x7F]', '', normalized)
        safe_message = escape(safe_message)

        address_pattern = r"([a-z0-9]+\.b32\.i2p|[a-z0-9]+\.i2p)"
        return re.sub(address_pattern, r"[bold cyan]\1[/]", safe_message)


    def parse_reply_text(self, message: str):
        value = str(message)
        prefix = f"{REPLY_BEGIN_MARKER}\n"
        if not value.startswith(prefix):
            return None

        rest = value[len(prefix):]
        author, separator, rest = rest.partition("\n")
        if not separator:
            return None

        quote_prefix = f"{REPLY_QUOTE_MARKER}\n"
        if not rest.startswith(quote_prefix):
            return None

        rest = rest[len(quote_prefix):]
        end_marker = f"\n{REPLY_END_MARKER}\n"
        quote, separator, body = rest.partition(end_marker)
        if not separator:
            return None

        return {
            "author": author,
            "quote": quote,
            "body": body,
        }


    def render_text_message_content(self, message: str):
        reply = self.parse_reply_text(message)
        if not reply:
            return f"[white]{self.format_chat_message(message)}[/]"

        content = Table.grid(expand=False)
        content.add_column()

        quote_header = self.format_chat_message(f"Reply to {reply['author']}")
        quote_text = self.format_chat_message(reply["quote"])
        quote_panel = Panel(
            f"[dim]{quote_header}[/]\n[bright_black]{quote_text}[/]",
            border_style="bright_black",
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False,
        )
        content.add_row(quote_panel)
        content.add_row(Text.from_markup(f"[white]{self.format_chat_message(reply['body'])}[/]"))
        return content


    def render_chat_entry(self, entry):
        if entry.get("kind") == "bubble":
            type_name = entry["type"]
            message_content = self.render_text_message_content(entry["message"])

            if type_name == "me":
                box_color = "green"
                display_name = "Me"
                alignment = "left"
            elif type_name == "peer":
                box_color = "cyan"
                display_name = "Peer"
                alignment = "right"
            elif type_name == "me_offline":
                box_color = "yellow"
                display_name = "Me-Offline"
                alignment = "left"
            else:
                box_color = "magenta"
                display_name = "Peer-Offline"
                alignment = "right"

            delivery = ""
            if entry.get("msg_id") is not None and entry.get("delivered"):
                mark = "✓" if type_name == "me_offline" else "✓✓"
                delivery = f" [dim green]{mark}[/]"

            message_panel = Panel(
                message_content,
                title=f"[#5f5f5f][{entry['timestamp']} UTC][/] [bold {box_color}]{display_name}[/]{delivery}",
                title_align="left",
                border_style=box_color,
                box=box.ROUNDED,
                expand=False
            )

            return Align(message_panel, align=alignment), True

        if entry.get("kind") == "image":
            delivery = ""
            expected = entry.get("group_expected_acks") or []
            received = entry.get("group_received_acks") or []
            if expected:
                mark = "✓✓" if len(received) >= len(expected) else "✓"
                delivery = f" [dim green]{mark} {len(received)}/{len(expected)}[/]"
            elif entry.get("msg_id") is not None and entry.get("delivered"):
                delivery = " [dim green]✓✓[/]"

            image_content = Text.from_markup(entry["content"]) if entry.get("markup") else Text(entry["content"], style="bright_white")

            message_panel = Panel(
                image_content,
                title=f"[#5f5f5f][{entry['timestamp']} UTC][/] [bold {entry['color']}]{entry['display']}[/]{delivery}",
                title_align="left",
                border_style=entry["color"],
                box=box.ROUNDED,
                expand=False
            )

            return Align(message_panel, align=entry["alignment"]), True

        if entry.get("kind") == "group_bubble":
            message_content = self.render_text_message_content(entry["message"])
            mine = bool(entry.get("mine"))
            box_color = "green" if mine else "cyan"
            display_name = "Me" if mine else entry.get("author", "Group")
            alignment = "left" if mine else "right"

            delivery = ""
            if mine:
                expected = entry.get("group_expected_acks") or []
                received = entry.get("group_received_acks") or []
                if expected:
                    mark = "✓✓" if len(received) >= len(expected) else "✓"
                    delivery = f" [dim green]{mark} {len(received)}/{len(expected)}[/]"

            message_panel = Panel(
                message_content,
                title=f"[#5f5f5f][{entry['timestamp']} UTC][/] [bold {box_color}]{display_name}[/]{delivery}",
                title_align="left",
                border_style=box_color,
                box=box.ROUNDED,
                expand=False
            )

            return Align(message_panel, align=alignment), True

        return entry["content"], False


    def append_chat_entry(self, entry):
        self.chat_history.append(entry)
        renderable, expand = self.render_chat_entry(entry)
        widget = ChatEntryWidget(entry, renderable, expand=expand)
        entry["_widget"] = widget
        self.chat_log.mount(widget)
        self.call_after_refresh(lambda: self.chat_log.scroll_end(animate=False))
        return entry


    def rerender_chat_history(self):
        self.chat_log.remove_children()
        for entry in self.chat_history:
            renderable, expand = self.render_chat_entry(entry)
            widget = ChatEntryWidget(entry, renderable, expand=expand)
            entry["_widget"] = widget
            self.chat_log.mount(widget)
        self.call_after_refresh(lambda: self.chat_log.scroll_end(animate=False))


    def refresh_chat_entry(self, entry):
        widget = entry.get("_widget")
        if not widget:
            self.rerender_chat_history()
            return
        renderable, expand = self.render_chat_entry(entry)
        widget.expand = expand
        widget.update(renderable)


    def mark_chat_entry_delivered(self, entry):
        entry["delivered"] = True
        self.refresh_chat_entry(entry)


    def append_log_entry(self, content: str):
        self.log_history.append(content)
        return content


    async def send_direct_message(self, message: str):
        if self.sam_runtime and self.sam_runtime.is_closing():
            self.post("error", "Cannot send while chat is closing.")
            return
        current_task = asyncio.current_task()
        if self.sam_runtime and current_task:
            self.sam_runtime.track_send_task(current_task)

        msg_id = None
        try:
            _, writer = self.conn

            cipher = self.e2e.encrypt(message.encode())
            frame = self.frame_message('U', cipher)
            msg_id = struct.unpack(">Q", frame[6:14])[0]
            pending_entry = {
                "kind": "bubble",
                "type": "me",
                "message": message,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "msg_id": msg_id,
                "delivered": False,
            }
            self.pending_messages[msg_id] = pending_entry

            writer.write(frame)
            await writer.drain()

            self.append_chat_entry(pending_entry)
        except Exception:
            if msg_id is not None:
                self.pending_messages.pop(msg_id, None)
            self.post("error", "Failed to send message.")
            self.conn = None
            self.live_ready = False


    async def send_offline_message(self, message: str):
        if not self.offline_ready() or not self.offline_mode:
            self.post("error", "Offline mode is not ready.")
            return

        try:
            blob_key = self.get_offline_blob_key()
            frame = self.frame_message('U', message.encode())
            msg_id = struct.unpack(">Q", frame[6:14])[0]
            blob = self.e2e.encrypt_offline_blob(frame, blob_key)
            pending_entry = {
                "kind": "bubble",
                "type": "me_offline",
                "message": message,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "msg_id": msg_id,
                "delivered": False,
            }
            self.append_chat_entry(pending_entry)

            send_index = self.drop_send_index
            dd_key = self.derive_deaddrop_key("send", send_index)

            status, ok_drops = await self.deaddrop.put(dd_key, blob)

            if status in ("OK", "EXISTS"):
                if ok_drops:
                    self.prefer_deaddrop_server(ok_drops[0])

                self.drop_send_index += 1
                self.save_offline_state()
                self.set_dd_status("put_ok")
                self.mark_chat_entry_delivered(pending_entry)
            else:
                self.set_dd_status("put_fail")
                self.post("error", "[OFFLINE send failed] deaddrop PUT did not succeed")
        except Exception as e:
            self.set_dd_status("put_fail")
            self.post("error", f"[OFFLINE send failed] {e}")


    def show_logs(self):
        self.push_screen(LogsScreen(list(self.log_history)))


    def open_profiles_screen(self):
        self.push_screen(ProfilesScreen(list_persistent_profile_rows()))


    def post(self, type_name: str, message: str, msg_id=None):
        
        styles = {
            "info": "[bold blue]STATUS:[/] [white]{}[/]",
            "status": "[bold blue]STATUS:[/] [white]{}[/]",
            "error": "[bold red]ERROR:[/] [red]{}[/]",
            #"system": "[bold yellow]SYSTEM:[/] [italic gray]{}[/]",
            "system": "[#878700]SYSTEM:[/] [dim #9f9f9f italic]{}[/]",
            "me": "[bold green]Me:[/] [white]{}[/]",
            "me_offline": "[bold yellow]Me-Offline:[/] [white]{}[/]",
            "peer_offline": "[bold magenta]Peer-Offline:[/] [white]{}[/]",
            "peer": "[bold cyan]Peer:[/] [white]{}[/]",
            "success": "[bold green]✔[/] [white]{}[/]",
            "disconnect": "[bold red]X[/] [white]{}[/]",
            "help": "[dim]HELP:[/] [gray62]{}[/]",
            "help_bold": "[dim]HELP:[/] [gray62 bold]{}[/]"
        }
        
        
        formatted_msg = self.format_chat_message(message)

        
        content = styles.get(type_name, "{}").format(formatted_msg)
        
        
        
        if type_name in ["me", "peer", "me_offline", "peer_offline"]:
            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
            return self.append_chat_entry({
                "kind": "bubble",
                "type": type_name,
                "message": message,
                "timestamp": now_utc,
                "msg_id": msg_id,
                "delivered": False,
            })

        self.append_log_entry(content)
        return content
            
            
    
    def frame_message(self, msg_type: str, payload, msg_id=None):

        if isinstance(payload, str):
            payload = payload.encode()

        if msg_id is None:
            msg_id = self.generate_msg_id()

        header = struct.pack(">4sBcQI", MAGIC, PROTOCOL_VERSION, msg_type.encode(), msg_id, len(payload))
        

        return header + payload
    
    
    
    async def read_frame(self, reader):

        # MAGIC search
        buffer = b""

        while True:

            b = await reader.readexactly(1)
            buffer += b

            if buffer.endswith(MAGIC):
                break

            if len(buffer) > 4:
                buffer = buffer[-4:]

        # Read rest of header
        header = await reader.readexactly(14)

        version, msg_type, msg_id, length = struct.unpack(">BcQI", header)
        
        # Protocol version/frame/type check
        if version != PROTOCOL_VERSION:
            raise ValueError("Unsupported protocol version")
        
        if msg_type not in b"UDSFCEKPOXLQYJGZ":
            raise ValueError("Unknown frame type")

    
        if length < 0 or length > MAX_FRAME_SIZE:
            raise ValueError("Invalid frame size")

        payload = await reader.readexactly(length)

        return msg_type.decode(), msg_id, payload
    
    
    
    
    def parse_frame_bytes(self, frame: bytes):
        if len(frame) < 18:
            raise ValueError("Frame too short")

        magic, version, msg_type, msg_id, length = struct.unpack(">4sBcQI", frame[:18])

        if magic != MAGIC:
            raise ValueError("Invalid frame MAGIC")

        if version != PROTOCOL_VERSION:
            raise ValueError("Unsupported protocol version")

        if msg_type not in b"UDSFCEKPOXLQYJGZ":
            raise ValueError("Unknown frame type")

        if length < 0 or length > MAX_FRAME_SIZE:
            raise ValueError("Invalid frame size")

        if len(frame) != 18 + length:
            raise ValueError("Frame length mismatch")

        payload = frame[18:18 + length]

        return msg_type.decode(), msg_id, payload
    
    
    
    def generate_msg_id(self):
        return (int(time.time() * 1000) ^ random.getrandbits(32)) & 0xFFFFFFFFFFFFFFFF


    def image_mime_for_path(self, path: str):
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mapping = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "webp": "image/webp",
        }
        return mapping.get(ext)


    def is_supported_image_mime(self, mime: str) -> bool:
        return mime in {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}


    def prepare_image_preview_bytes(self, path: str):
        if not os.path.exists(path):
            raise RuntimeError(f"File not found: {path}")

        source_size = os.path.getsize(path)
        if source_size <= 0:
            raise RuntimeError("Image is empty.")
        if source_size > MAX_FILE_SIZE:
            raise RuntimeError(
                f"Image source is too large for inline preview ({source_size} bytes). Use /sendfile for the original."
            )

        try:
            decoded = Image.open(path)
            decoded.load()
        except Exception as e:
            raise RuntimeError(f"Image decode failed: {e}")

        keep_alpha = (
            decoded.mode in ("RGBA", "LA")
            or (decoded.mode == "P" and "transparency" in decoded.info)
        )

        preview = decoded.copy()
        if preview.width > IMAGE_TRANSFER_MAX_DIMENSION or preview.height > IMAGE_TRANSFER_MAX_DIMENSION:
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            preview.thumbnail(
                (IMAGE_TRANSFER_MAX_DIMENSION, IMAGE_TRANSFER_MAX_DIMENSION),
                resampling,
            )

        out = BytesIO()
        if keep_alpha:
            preview.convert("RGBA").save(out, format="PNG")
            return out.getvalue(), "image/png"

        preview.convert("RGB").save(
            out,
            format="JPEG",
            quality=IMAGE_TRANSFER_JPEG_QUALITY,
        )
        return out.getvalue(), "image/jpeg"


    def image_suffix_for_mime(self, mime: str) -> str:
        mapping = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/webp": ".webp",
        }
        return mapping.get(mime, ".img")


    def render_image_bytes_for_terminal(self, image_bytes: bytes, mime: str, mode: str = "braille") -> str:
        secure_makedirs(IMAGE_DIR)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                dir=IMAGE_DIR,
                suffix=self.image_suffix_for_mime(mime),
            ) as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name

            try:
                os.chmod(tmp_path, FILE_MODE)
            except:
                pass

            lines = render_bw(tmp_path, width=IMAGE_RENDER_WIDTH) if mode == "bw" else render_braille_color(tmp_path, width=IMAGE_RENDER_WIDTH)
            if len(lines) > MAX_IMAGE_LINES:
                raise RuntimeError("Image too large to render safely")
            return "\n".join(lines)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except:
                    pass


    def clear_incoming_image_state(self):
        self.incoming_image_name = None
        self.incoming_image_mime = None
        self.incoming_image_expected = 0
        self.incoming_image_received = 0
        self.incoming_image_msg_id = 0
        self.incoming_image_bytes = bytearray()


    def peer_dest_fingerprint(self, dest_b64: str) -> str:
        return hashlib.sha256(dest_b64.encode()).hexdigest()[:16]


    def sam_session_label(self, profile_name: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in profile_name)


    def peer_dest_matches_tofu(self, dest_b64: str) -> bool:
        if not self.stored_peer_dest_b64:
            return True
        return dest_b64 == self.stored_peer_dest_b64


    def set_tofu_verified(self):
        self.tofu_verified = True
        self.tofu_mismatch = False
        self.watch_peer_b32(self.peer_b32)


    def set_tofu_mismatch(self):
        self.tofu_verified = False
        self.tofu_mismatch = True
        self.watch_peer_b32(self.peer_b32)


    def clear_tofu_runtime_status(self):
        self.tofu_verified = False
        self.tofu_mismatch = False
        self.watch_peer_b32(self.peer_b32)
        
        
    def open_file_picker(self, image_mode: str | None = None):
        image_extensions = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
        allowed = image_extensions if image_mode else None
        title = "Choose image" if image_mode else "Choose file"

        def handle_selection(path):
            if not path:
                return

            if image_mode:
                if self.active_group:
                    self.run_worker(self.send_group_image(path, mode=image_mode))
                elif self.conn:
                    self.run_worker(self.send_image(path, mode=image_mode))
                else:
                    self.post("error", "No active connection. Use /connect <address>.")
            else:
                if not self.conn:
                    self.post("error", "No active connection. Use /connect <address>.")
                    return
                self.run_worker(self.send_file(path))

        self.push_screen(
            FilePickerScreen(title, allowed_extensions=allowed),
            callback=handle_selection,
        )
        
        
        
        
    def mark_live_ready_if_needed(self):
        if self.e2e.ready() and not self.live_ready:
            self.live_ready = True
            self.pq_active = self.pq_enabled
            self.start_heartbeat()
            self.watch_peer_b32(self.peer_b32)
            
            self.update_command_bar()
            
            self.post("system", "Secure session established 🔐")

            if self.offline_ready():
                task = asyncio.create_task(self.sync_offline_secret_if_needed())
                self.sam_runtime.track_send_task(task)

            if self.is_persistent_mode():
                task = asyncio.create_task(self.send_deaddrop_server_list())
                self.sam_runtime.track_send_task(task)
        
        


    def heartbeat_nonce(self) -> str:
        return f"{self.generate_msg_id():016x}"


    def reset_heartbeat_state(self):
        self.heartbeat_last_rx_ts = 0.0
        self.heartbeat_last_ping_ts = 0.0


    def mark_heartbeat_rx(self):
        self.heartbeat_last_rx_ts = time.monotonic()


    def stop_heartbeat(self):
        if self.heartbeat_task:
            try:
                self.heartbeat_task.cancel()
            except:
                pass
            self.heartbeat_task = None
        self.reset_heartbeat_state()


    def start_heartbeat(self):
        if not self.conn or not self.live_ready:
            return

        now = time.monotonic()
        if self.heartbeat_last_rx_ts == 0.0:
            self.heartbeat_last_rx_ts = now
        if self.heartbeat_last_ping_ts == 0.0:
            self.heartbeat_last_ping_ts = now

        if self.heartbeat_task and not self.heartbeat_task.done():
            return

        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(self.conn))


    async def heartbeat_loop(self, connection):
        try:
            while self.conn == connection:
                await asyncio.sleep(1.0)

                if self.conn != connection:
                    break
                if not self.live_ready:
                    continue

                reader, writer = connection
                if writer.is_closing():
                    break

                now = time.monotonic()
                if self.heartbeat_last_rx_ts == 0.0:
                    self.heartbeat_last_rx_ts = now

                if now - self.heartbeat_last_rx_ts >= HEARTBEAT_TIMEOUT:
                    self.post("disconnect", "Peer heartbeat timed out.")
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except:
                        pass
                    break

                if (
                    now - self.heartbeat_last_ping_ts >= HEARTBEAT_PING_INTERVAL
                    and now - self.heartbeat_last_rx_ts >= HEARTBEAT_PING_INTERVAL
                ):
                    self.heartbeat_last_ping_ts = now
                    writer.write(
                        self.frame_message(
                            'S',
                            f"{HEARTBEAT_PING_PREFIX}{self.heartbeat_nonce()}"
                        )
                    )
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if self.heartbeat_task == asyncio.current_task():
                self.heartbeat_task = None


    def clear_pending_incoming(self):
        self.pending_incoming_conn = None
        self.pending_incoming_addr = None
        self.pending_incoming_dest_b64 = None
        self.watch_peer_b32(self.peer_b32)
        
        
        
    def pending_incoming_is_dead(self) -> bool:
        if not self.pending_incoming_conn:
            return True

        reader, writer = self.pending_incoming_conn

        try:
            if writer.is_closing():
                return True
        except:
            return True

        try:
            if reader.at_eof():
                return True
        except:
            return True

        return False
        
    
    
    async def pending_receive_loop(self, connection):
        reader, writer = connection

        try:
            while self.pending_incoming_conn == connection:
                try:
                    msg_type, msg_id, payload = await self.read_frame(reader)

                    if msg_type not in ('K', 'P', 'O', 'S', 'D', 'Z'):
                        payload = self.e2e.decrypt(payload)

                except UnicodeDecodeError:
                    continue
                except ValueError:
                    continue

                await self.handle_parsed_frame(
                    msg_type,
                    msg_id,
                    payload,
                    writer=writer,
                    source="pending"
                )

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass

        except asyncio.CancelledError:
            pass

        except Exception as e:
            if self.pending_incoming_conn == connection:
                self.post("error", f"Pending call error: {e}")

        finally:
            if not self.promoting_pending_incoming:
                if self.pending_incoming_conn == connection:
                    caller = self.pending_incoming_addr or "Unknown"
                    
                    self.clear_pending_incoming()
                    self.current_peer_addr = None
                    self.current_peer_dest_b64 = None
                    self.peer_b32 = "Waiting for incoming connections..."
                    self.clear_tofu_runtime_status()
                    self.pq_active = False
                    
                    self.update_command_bar()
                    
                    self.post("system", f"Incoming caller disconnected: {caller[:12]}...")

                    try:
                        writer.close()
                        await writer.wait_closed()
                    except:
                        pass

            self.pending_incoming_task = None
    
    
    
    async def accept_pending_incoming(self):
        if not self.pending_incoming_conn:
            self.post("error", "No incoming call to accept.")
            return

        if self.pending_incoming_is_dead():
            
            self.clear_pending_incoming()
            self.current_peer_addr = None
            self.current_peer_dest_b64 = None
            self.peer_b32 = "Waiting for incoming connections..."
            self.clear_tofu_runtime_status()
            self.pq_active = False
            
            self.update_command_bar()
            
            self.post("error", "Incoming caller disconnected.")
            return

        reader, writer = self.pending_incoming_conn
        accepted_from = self.pending_incoming_addr or "Unknown"
        accepted_dest_b64 = self.pending_incoming_dest_b64

        self.promoting_pending_incoming = True

        task = self.pending_incoming_task
        self.pending_incoming_task = None

        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except:
                pass

        self.clear_pending_incoming()

        self.current_peer_addr = accepted_from
        self.current_peer_dest_b64 = accepted_dest_b64
        self.peer_b32 = accepted_from or "Unknown"

        if accepted_dest_b64 and self.stored_peer_dest_b64:
            self.set_tofu_verified()
        else:
            self.clear_tofu_runtime_status()

        if hasattr(self, 'my_pub_dest_b64'):
            writer.write(self.frame_message('S', self.my_pub_dest_b64))
            await writer.drain()

            writer.write(self.frame_message('K', self.e2e.public_bytes()))
            await writer.drain()
            
            if self.pq_enabled:
                writer.write(self.frame_message('Q', self.e2e.pq_public_bytes()))
                await writer.drain()

        if self.offline_mode:
            self.leave_offline_mode()
            self.watch_peer_b32(self.peer_b32)
            self.post("system", "Leaving OFFLINE mode due to accepted live incoming connection.")

        self.conn = (reader, writer)
        self.live_ready = self.e2e.ready()
        self.start_heartbeat()
        
        self.watch_peer_b32(self.peer_b32)
        self.update_command_bar()

        self.promoting_pending_incoming = False

        self.post("success", f"Accepted incoming call from {accepted_from[:12]}...")

        if self.live_ready:
            self.post("system", "Secure session established 🔐")

        self.run_worker(self.receive_loop(self.conn))
    


    async def decline_pending_incoming(self):
        if not self.pending_incoming_conn:
            self.post("error", "No incoming call to decline.")
            return

        if self.pending_incoming_is_dead():
            
            self.clear_pending_incoming()
            self.current_peer_addr = None
            self.current_peer_dest_b64 = None
            self.peer_b32 = "Waiting for incoming connections..."
            self.clear_tofu_runtime_status()
            self.pq_active = False
            
            self.update_command_bar()
            
            self.post("system", "Incoming caller already disconnected.")
            return

        _, writer = self.pending_incoming_conn
        declined_from = self.pending_incoming_addr or "Unknown"

        self.promoting_pending_incoming = True

        task = self.pending_incoming_task
        self.pending_incoming_task = None

        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except:
                pass

        self.clear_pending_incoming()
        self.current_peer_addr = None
        self.current_peer_dest_b64 = None
        self.peer_b32 = "Waiting for incoming connections..."
        self.clear_tofu_runtime_status()
        self.pq_active = False
        
        self.update_command_bar()

        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass

        self.promoting_pending_incoming = False

        self.post("system", f"Declined incoming call from {declined_from[:12]}...")



    async def call_blink_worker(self):
        while True:
            try:
                if self.pending_incoming_conn:
                    self.call_blink_on = not self.call_blink_on
                    self.watch_peer_b32(self.peer_b32)
                else:
                    if not self.call_blink_on:
                        self.call_blink_on = True
                        self.watch_peer_b32(self.peer_b32)
            except:
                pass

            await asyncio.sleep(1)



    async def pending_incoming_watch_worker(self):
        while True:
            try:
                if self.pending_incoming_conn and self.pending_incoming_is_dead():
                    caller = self.pending_incoming_addr or "Unknown"
                    self.clear_pending_incoming()
                    self.post("system", f"Incoming caller disconnected: {caller[:12]}...")
            except:
                pass

            await asyncio.sleep(1)




    def set_dd_status(self, status: str):
        self.dd_status = status
        self.dd_status_ts = time.time()
        self.watch_peer_b32(self.peer_b32)


    def get_dd_status_label(self) -> str:
        if not self.offline_mode:
            return ""

        age = time.time() - self.dd_status_ts if self.dd_status_ts else 9999

        if age > 8:
            return "[black on grey62] DD IDLE [/]"

        mapping = {
            "idle": "[black on grey62] DD IDLE [/]",
            "poll": "[black on yellow] DD POLL [/]",
            "put_ok": "[black on green] DD PUT [/]",
            "put_fail": "[black on red] DD FAIL [/]",
            "get_hit": "[black on magenta] DD HIT [/]",
            "get_miss": "[black on grey62] DD MISS [/]",
            "get_fail": "[black on red] DD FAIL [/]",
        }

        return mapping.get(self.dd_status, "[black on grey62] DD IDLE [/]")



    def get_offline_peer_b32(self):
        peer = self.stored_peer or self.current_peer_addr
        if not peer:
            return None
        return peer.replace(".b32.i2p", "").strip().lower()


    def derive_deaddrop_key(self, direction: str, index: int) -> str:
        if not hasattr(self, "my_b32"):
            raise RuntimeError("Local destination not ready")

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            raise RuntimeError("Peer address not known for deaddrop key derivation")

        my_b32 = self.my_b32.replace(".b32.i2p", "").strip().lower()

        low_id, high_id = sorted([my_b32, peer_b32])

        if my_b32 == low_id:
            send_label = "LOW_TO_HIGH"
            recv_label = "HIGH_TO_LOW"
        else:
            send_label = "HIGH_TO_LOW"
            recv_label = "LOW_TO_HIGH"

        if direction == "send":
            dir_label = send_label
        elif direction == "recv":
            dir_label = recv_label
        else:
            raise ValueError("direction must be 'send' or 'recv'")

        material = b"|".join([
            self.offline_shared_secret,
            low_id.encode(),
            high_id.encode(),
            dir_label.encode(),
            str(index).encode(),
        ])

        return hashlib.sha256(material).hexdigest()


    def next_deaddrop_send_key(self) -> str:
        key = self.derive_deaddrop_key("send", self.drop_send_index)
        self.drop_send_index += 1
        return key


    def get_deaddrop_recv_window(self):
        keys = []
        for i in range(self.drop_recv_base, self.drop_recv_base + self.drop_window):
            if i in self.consumed_drop_recv:
                continue
            keys.append((i, self.derive_deaddrop_key("recv", i)))
        return keys


    def advance_drop_recv_base(self):
        while self.drop_recv_base in self.consumed_drop_recv:
            self.drop_recv_base += 1



    def get_offline_blob_key(self):
        if not hasattr(self, "my_b32"):
            raise RuntimeError("Local destination not ready")

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            raise RuntimeError("Peer address not known for offline blob key")

        my_b32 = self.my_b32.replace(".b32.i2p", "").strip().lower()

        return self.e2e.derive_offline_blob_key(
            self.offline_shared_secret,
            my_b32,
            peer_b32
        )



    def is_persistent_mode(self) -> bool:
        return self.profile != "default"


    def offline_ready(self) -> bool:
        return (
            self.is_persistent_mode()
            and bool(self.stored_peer)
            and self.deaddrop_enabled
        )
    
    def leave_offline_mode(self):
        self.offline_mode = False
        self.dd_status = "idle"
        self.dd_status_ts = 0.0
        self.watch_peer_b32(self.peer_b32)
    
    
    def offline_state_path(self) -> str:
        peer = self.get_offline_peer_b32()
        if not peer:
            raise RuntimeError("Locked peer not available for offline state path")

        safe_peer = peer.replace("/", "_")
        return os.path.join(PROFILE_DIR, f"offline_{safe_peer}.state")


    def load_offline_state(self):
        if not self.offline_ready():
            return

        path = self.offline_state_path()

        if not os.path.exists(path):
            return

        try:
            with open(path, "r") as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]

            data = {}

            for line in lines:
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()

            if "offline_shared_secret" in data:
                try:
                    self.offline_shared_secret = bytes.fromhex(data["offline_shared_secret"])
                except:
                    self.offline_shared_secret = b"CHANGE_ME_SHARED_OFFLINE_SECRET"

            if "drop_send_index" in data:
                self.drop_send_index = int(data["drop_send_index"])

            if "drop_recv_base" in data:
                self.drop_recv_base = int(data["drop_recv_base"])

            if "drop_window" in data:
                self.drop_window = int(data["drop_window"])

            if "consumed_drop_recv" in data and data["consumed_drop_recv"]:
                self.consumed_drop_recv = set(
                    int(x) for x in data["consumed_drop_recv"].split(",") if x.strip()
                )
            else:
                self.consumed_drop_recv = set()

            self.post("system", f"Loaded offline state for {self.stored_peer}")

        except Exception as e:
            self.post("error", f"Failed to load offline state: {e}")


    def save_offline_state(self):
        if not self.offline_ready():
            return

        try:
            path = self.offline_state_path()

            content = ""
            content += f"offline_shared_secret={self.offline_shared_secret.hex()}\n"
            content += f"drop_send_index={self.drop_send_index}\n"
            content += f"drop_recv_base={self.drop_recv_base}\n"
            content += f"drop_window={self.drop_window}\n"
            content += "consumed_drop_recv=" + ",".join(str(x) for x in sorted(self.consumed_drop_recv)) + "\n"

            secure_write_text_atomic(path, content)

        except Exception as e:
            self.post("error", f"Failed to save offline state: {e}")


    
    def clear_offline_state_file(self):
        try:
            path = self.offline_state_path()
            if os.path.exists(path):
                os.remove(path)
        except:
            pass



    def reset_peer_binding_state(self):
        self.clear_offline_state_file()

        self.stored_peer = None
        self.stored_peer_dest_b64 = None
        self.current_peer_addr = None
        self.current_peer_dest_b64 = None

        self.offline_shared_secret = b"CHANGE_ME_SHARED_OFFLINE_SECRET"
        self.drop_send_index = 0
        self.drop_recv_base = 0
        self.drop_window = 8
        self.consumed_drop_recv = set()

        self.offline_mode = False
        self.seen_drop_msgs = set()


    
    def has_real_offline_secret(self) -> bool:
        return self.offline_shared_secret != b"CHANGE_ME_SHARED_OFFLINE_SECRET"


    def generate_offline_shared_secret(self) -> bytes:
        return os.urandom(32)
    
    
    def should_initiate_offline_secret(self) -> bool:
        if not hasattr(self, "my_b32"):
            return False

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            return False

        
        my_b32 = self.my_b32.replace(".b32.i2p", "").strip().lower()
        peer_b32 = peer_b32.strip().lower()

        return my_b32 < peer_b32


    async def send_offline_secret_if_needed(self):
        if not self.conn:
            return

        if not self.live_ready:
            return

        if not self.offline_ready():
            return

        if not self.should_initiate_offline_secret():
            return

        try:
            _, writer = self.conn

            if self.has_real_offline_secret():
                secret = self.offline_shared_secret
                self.post("system", "Offline secret sync sent.")
            else:
                self.offline_shared_secret = self.generate_offline_shared_secret()
                self.save_offline_state()
                secret = self.offline_shared_secret
                self.post("system", "Offline secret generated and saved.")

            writer.write(self.frame_message('X', secret))
            await writer.drain()

            self.post("system", "Offline secret sent to locked peer.")
        except Exception as e:
            self.post("error", f"Failed to send offline secret: {e}")


    async def request_offline_secret_if_needed(self):
        if not self.conn:
            return

        if not self.live_ready:
            return

        if not self.offline_ready():
            return

        if self.has_real_offline_secret():
            return

        if self.should_initiate_offline_secret():
            return

        try:
            _, writer = self.conn
            writer.write(self.frame_message('S', OFFLINE_SECRET_REQUEST_SIGNAL))
            await writer.drain()
            self.post("system", "Requesting offline secret sync from peer.")
        except Exception as e:
            self.post("error", f"Failed to request offline secret: {e}")


    async def sync_offline_secret_if_needed(self):
        if not self.conn or not self.live_ready or not self.offline_ready():
            return

        if self.should_initiate_offline_secret():
            await self.send_offline_secret_if_needed()
        else:
            await self.request_offline_secret_if_needed()



    async def ensure_offline_runtime_started(self):
        if not self.offline_ready():
            return

        if not self.deaddrop_started:
            await self.deaddrop.start()
            self.deaddrop_started = True
            # Deaddrop test
            #asyncio.create_task(self.test_drop())

        if not self.deaddrop_poller_started:
            self.run_worker(self.poll_deaddrops())
            self.deaddrop_poller_started = True




    def ensure_profile_deaddrop_servers_file(self):
        if not self.is_persistent_mode():
            return

        path = self.deaddrop_servers_path()
        if os.path.exists(path):
            return

        if not os.path.exists(DD_BOOTSTRAP_FILE):
            return

        try:
            with open(DD_BOOTSTRAP_FILE, "r") as f:
                content = f.read()

            secure_write_text(path, content)
            self.post("system", "Initialized deaddrop server list from bootstrap defaults.")
            
        except Exception as e:
            self.post("error", f"Failed to initialize deaddrop server list from bootstrap: {e}")




    def deaddrop_servers_path(self) -> str:
        return os.path.join(PROFILE_DIR, "deaddrop_servers.txt")
    
    
    
    def deaddrop_stats_path(self) -> str:
        return os.path.join(PROFILE_DIR, "deaddrop_stats.json")
    
    
    
    def _default_deaddrop_stat(self):
        return {
            "put_ok": 0,
            "put_fail": 0,
            "get_ok": 0,
            "get_fail": 0,
            "last_success_ts": 0.0,
            "latency_ema_ms": 0.0,
            "latency_samples": 0,
        }

    def load_deaddrop_stats(self):
        if not self.is_persistent_mode():
            return

        path = self.deaddrop_stats_path()
        if not os.path.exists(path):
            self.deaddrop_stats = {}
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            if isinstance(raw, dict):
                cleaned = {}
                for server, stats in raw.items():
                    if not self.is_valid_deaddrop_server(server):
                        continue

                    base = self._default_deaddrop_stat()
                    if isinstance(stats, dict):
                        for k in base.keys():
                            if k in stats:
                                base[k] = stats[k]
                    cleaned[server] = base

                self.deaddrop_stats = cleaned
            else:
                self.deaddrop_stats = {}

        except Exception as e:
            self.deaddrop_stats = {}
            self.post("error", f"Failed to load deaddrop stats: {e}")

    def save_deaddrop_stats(self):
        if not self.is_persistent_mode():
            return

        try:
            path = self.deaddrop_stats_path()
            content = json.dumps(self.deaddrop_stats, indent=2, sort_keys=True)
            secure_write_text_atomic(path, content)
        except Exception as e:
            self.post("error", f"Failed to save deaddrop stats: {e}")
            
            
            
    def flush_deaddrop_stats_if_needed(self, force: bool = False):
        if not self.is_persistent_mode():
            return

        if not self.deaddrop_stats_dirty and not force:
            return

        now = time.time()

        if not force and (now - self.deaddrop_stats_last_save_ts) < DD_STATS_SAVE_INTERVAL:
            return

        self.save_deaddrop_stats()
        self.deaddrop_stats_dirty = False
        self.deaddrop_stats_last_save_ts = now
            
            

    def ensure_deaddrop_stat_entry(self, server: str):
        if server not in self.deaddrop_stats:
            self.deaddrop_stats[server] = self._default_deaddrop_stat()

    def record_deaddrop_stat(self, op: str, drop: str, ok: bool, latency_ms: float, detail: str):
        drop = drop.strip().lower()

        if not self.is_valid_deaddrop_server(drop):
            return

        self.ensure_deaddrop_stat_entry(drop)
        st = self.deaddrop_stats[drop]

        if op == "put":
            if ok:
                st["put_ok"] += 1
            else:
                st["put_fail"] += 1
        elif op == "get":
            if ok:
                st["get_ok"] += 1
            else:
                st["get_fail"] += 1
        else:
            return

        if ok:
            st["last_success_ts"] = time.time()

        if latency_ms > 0:
            if st["latency_samples"] <= 0 or st["latency_ema_ms"] <= 0:
                st["latency_ema_ms"] = float(latency_ms)
            else:
                alpha = DD_STATS_EMA_ALPHA
                st["latency_ema_ms"] = (alpha * float(latency_ms)) + ((1.0 - alpha) * float(st["latency_ema_ms"]))
            st["latency_samples"] += 1

        self.rank_deaddrop_servers()
        self.deaddrop_stats_dirty = True
        self.flush_deaddrop_stats_if_needed()
        
        
        
        
    def deaddrop_server_score(self, server: str) -> float:
        st = self.deaddrop_stats.get(server)
        if not st:
            return DD_UNKNOWN_SERVER_SCORE

        put_ok = float(st.get("put_ok", 0))
        put_fail = float(st.get("put_fail", 0))
        get_ok = float(st.get("get_ok", 0))
        get_fail = float(st.get("get_fail", 0))

        total_ok = put_ok + get_ok
        total_fail = put_fail + get_fail
        total = total_ok + total_fail

        if total <= 0:
            return DD_UNKNOWN_SERVER_SCORE

        success_ratio = total_ok / total

        latency = float(st.get("latency_ema_ms", 0.0))
        if latency <= 0:
            latency_component = 0.0
        else:
            latency_component = -latency

        failure_penalty = total_fail * DD_FAILURE_PENALTY

        last_success_ts = float(st.get("last_success_ts", 0.0))
        recency_bonus = last_success_ts / 1000000.0 if last_success_ts > 0 else 0.0

        return (success_ratio * 100000.0) + latency_component + recency_bonus - failure_penalty

    def rank_deaddrop_servers(self):
        if not self.deaddrop_servers:
            self.apply_deaddrop_replica_policy()
            return

        original_order = {server: i for i, server in enumerate(self.deaddrop_servers)}

        self.deaddrop_servers.sort(
            key=lambda s: (
                self.deaddrop_server_score(s),
                -original_order.get(s, 10**9),
            ),
            reverse=True,
        )

        self.apply_deaddrop_replica_policy()
        self.save_deaddrop_servers()
        
    
    
    
    def apply_deaddrop_replica_policy(self):
        active = list(self.deaddrop_servers[:MAX_ACTIVE_DEADDROP_REPLICAS])
        self.deaddrop.drops = active
    
    

    def load_deaddrop_servers(self):
        if not self.is_persistent_mode():
            return

        path = self.deaddrop_servers_path()
        if not os.path.exists(path):
            return

        try:
            with open(path, "r") as f:
                servers = [line.strip() for line in f.readlines() if line.strip()]

            # Deduplication and healthy order
            merged = []
            seen = set()
            for s in servers:
                s = s.strip().lower()
                if not self.is_valid_deaddrop_server(s):
                    continue
                if s not in seen:
                    seen.add(s)
                    merged.append(s)

            if merged:
                self.deaddrop_servers = merged
                self.apply_deaddrop_replica_policy()
                self.post(
                    "system",
                    f"Loaded {len(self.deaddrop_servers)} deaddrop servers "
                    f"(active replicas: {len(self.deaddrop.drops)})."
                )
        except Exception as e:
            self.post("error", f"Failed to load deaddrop servers: {e}")


    def save_deaddrop_servers(self):
        if not self.is_persistent_mode():
            return

        try:
            path = self.deaddrop_servers_path()
            
            content = "".join(s + "\n" for s in self.deaddrop_servers)
            
            secure_write_text_atomic(path, content)
            
        except Exception as e:
            self.post("error", f"Failed to save deaddrop servers: {e}")


    def merge_deaddrop_servers(self, new_servers):
        changed = False

        for s in new_servers:
            s = s.strip().lower()

            if not self.is_valid_deaddrop_server(s):
                continue

            if s not in self.deaddrop_servers:
                self.deaddrop_servers.append(s)
                self.ensure_deaddrop_stat_entry(s)
                changed = True

        if changed:
            self.rank_deaddrop_servers()
            self.deaddrop_stats_dirty = True
            self.flush_deaddrop_stats_if_needed(force=True)

        return changed


    def prefer_deaddrop_server(self, server: str):
        if server in self.deaddrop_servers:
            self.ensure_deaddrop_stat_entry(server)

            st = self.deaddrop_stats[server]
            st["last_success_ts"] = max(float(st.get("last_success_ts", 0.0)), time.time())

            self.rank_deaddrop_servers()
            self.deaddrop_stats_dirty = True
            self.flush_deaddrop_stats_if_needed(force=True)



    def is_valid_deaddrop_server(self, server: str) -> bool:
        server = server.strip().lower()

        if not server:
            return False

        if not server.endswith(".b32.i2p"):
            return False

        host = server[:-8]  # strip ".b32.i2p"

        if len(host) not in (52, 56):
            return False

        allowed = set("abcdefghijklmnopqrstuvwxyz234567")
        return all(ch in allowed for ch in host)



    async def send_deaddrop_server_list(self):
        if not self.conn:
            return

        try:
            _, writer = self.conn
            payload = "\n".join(self.deaddrop_servers).encode()
            writer.write(self.frame_message('L', payload))
            await writer.drain()
            self.post("system", f"Shared {len(self.deaddrop_servers)} deaddrop servers with peer.")
        except Exception as e:
            self.post("error", f"Failed to share deaddrop servers: {e}")



    def deaddrop_server_lines(self):
        lines = [
            (
                "bold",
                f"Known deaddrop servers: {len(self.deaddrop_servers)} "
                f"(active replicas: {len(self.deaddrop.drops)})",
            )
        ]

        for i, s in enumerate(self.deaddrop_servers, start=1):
            st = self.deaddrop_stats.get(s, {})
            put_ok = st.get("put_ok", 0)
            put_fail = st.get("put_fail", 0)
            get_ok = st.get("get_ok", 0)
            get_fail = st.get("get_fail", 0)
            latency = st.get("latency_ema_ms", 0.0)

            active_tag = " *" if s in self.deaddrop.drops else ""
            lines.append((
                "line",
                f"  {i}. {s}{active_tag} "
                f"[put ok/fail={put_ok}/{put_fail} "
                f"get ok/fail={get_ok}/{get_fail} "
                f"lat={latency:.1f}ms]"
            ))

        return lines


    def deaddrop_server_rows(self):
        rows = []

        for i, server in enumerate(self.deaddrop_servers, start=1):
            st = self.deaddrop_stats.get(server, {})
            rows.append({
                "index": i,
                "server": server,
                "active": server in self.deaddrop.drops,
                "put_ok": st.get("put_ok", 0),
                "put_fail": st.get("put_fail", 0),
                "get_ok": st.get("get_ok", 0),
                "get_fail": st.get("get_fail", 0),
                "latency": st.get("latency_ema_ms", 0.0),
            })

        return rows


    def post_deaddrop_servers_to_chat(self):
        for _, text in self.deaddrop_server_lines():
            self.post("system", text)


    def show_deaddrop_servers(self):
        try:
            self.open_deaddrop_manager()
        except Exception:
            self.post_deaddrop_servers_to_chat()


    def get_deaddrop_server_by_index(self, index: int) -> str | None:
        if index < 1 or index > len(self.deaddrop_servers):
            return None
        return self.deaddrop_servers[index - 1]


    def open_deaddrop_manager(self, add_value: str = "", delete_index: int | None = None):
        self.push_screen(
            DeadDropManagerScreen(
                get_rows=self.deaddrop_server_rows,
                add_server=self.add_deaddrop_server,
                delete_server=self.delete_deaddrop_server_by_index,
                get_server=self.get_deaddrop_server_by_index,
                add_value=add_value,
                delete_index=delete_index,
            )
        )


    def add_deaddrop_server(self, server: str):
        server = server.strip().lower()

        if not self.is_valid_deaddrop_server(server):
            self.post("error", "Invalid deaddrop server address.")
            return

        if server in self.deaddrop_servers:
            self.post("error", "Deaddrop server already exists.")
            return

        self.deaddrop_servers.append(server)
        self.ensure_deaddrop_stat_entry(server)
        self.rank_deaddrop_servers()
        self.deaddrop_stats_dirty = True
        self.flush_deaddrop_stats_if_needed(force=True)
        self.post("success", f"Added deaddrop server: {server}")


    def delete_deaddrop_server_by_index(self, index: int):
        if index < 1 or index > len(self.deaddrop_servers):
            self.post("error", "Invalid deaddrop server number.")
            return

        removed = self.deaddrop_servers.pop(index - 1)

        if removed in self.deaddrop_stats:
            del self.deaddrop_stats[removed]

        self.apply_deaddrop_replica_policy()
        self.save_deaddrop_servers()
        self.deaddrop_stats_dirty = True
        self.flush_deaddrop_stats_if_needed(force=True)
        self.post("success", f"Removed deaddrop server: {removed}")





    async def on_mount(self):
        
        self.chat_log = self.query_one("#chat_window", ScrollableContainer)

        if self.app_mode == "groups":
            await self.on_mount_group_manager()
            return

        if self.app_mode == "group":
            await self.on_mount_group_chat()
            return
        
        self.network_status = "initializing"
        
        self.peer_b32 = "Initializing SAM Session..."
        
        
        self.post("system", f"{APP_NAME} {APP_VERSION}")
        self.post("system", "Initializing SAM Session...")
        self.post("system", f"Initializing Profile: {self.profile}")
        self.post("system", f"Post-quantum mode: {'ON' if self.pq_enabled else 'OFF'}")
        
        if RESET_PROFILE:
            self.post("system", f"Profile {self.profile} was reset before startup.")
        
        
        
        is_persistent = self.profile != "default"
        
        
        self.append_log_entry(
            f"[#878700]SYSTEM:[/] [dim #5f5f5f italic]Mode:[/][not bold {'yellow' if is_persistent else 'green'}] {'PERSISTENT' if is_persistent else 'TRANSIENT'}[/]"
        )

        
        
        key_file = os.path.join(PROFILE_DIR, f"{self.profile}.dat")

        try:
            
            my_dest_b64 = None
            my_pub_dest_b64 = None

            # Handle Persistence
            if is_persistent and os.path.exists(key_file):
                with open(key_file, "r") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]

                if len(lines) > 0:
                    my_dest_b64 = lines[0]
                    self.post("system", f"Loaded identity from {key_file}")

                if len(lines) > 1:
                    self.stored_peer = lines[1]
                    self.post("system", f"Locked Peer: {self.stored_peer}")

                if len(lines) > 2:
                    self.stored_peer_dest_b64 = lines[2]
                    fp = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                    self.post("system", f"TOFU peer pin loaded: {fp}")

            await self.sam_runtime.connect()

            # Generate new destination if needed
            if my_dest_b64 is None:
                self.post("system", "Generating new Ed25519 identity...")
                my_pub_dest_b64, my_dest_b64 = await self.sam_runtime.generate_destination(sig_type=7)

                if is_persistent:
                    
                    secure_write_text(key_file, my_dest_b64 + "\n")

                    self.post("success", f"Identity saved to {key_file}")
                    
            self.my_dest_b64 = my_dest_b64
            
            if my_pub_dest_b64:
                self.my_pub_dest_b64 = my_pub_dest_b64


            self.ensure_profile_deaddrop_servers_file()

            self.load_deaddrop_stats()
            self.load_deaddrop_servers()
            self.rank_deaddrop_servers()
            

            await self.sam_runtime.create_session(
                self.session_id,
                destination=my_dest_b64,
                options={
                    "inbound.length": "2",
                    "outbound.length": "2",
                    "inbound.quantity": "3",
                    "outbound.quantity": "3"
                }
            )

            self.my_pub_dest_b64 = await self.sam_runtime.naming_lookup("ME")
            self.my_b32 = self.sam.destination_to_b32(self.my_pub_dest_b64)
            


            self.network_status = "local_ok"
            #self.publish_ready = self.is_persistent_mode()
            self.publish_ready = False
            
            my_address = self.my_b32
            self.post("success", f"Online! My Address: {my_address}")

            self.peer_b32 = f"My Addr: {my_address}"
            
            if self.stored_peer:
                
                self.load_offline_state()
                
                self.post("system", "Type /connect to dial stored contact.")
                self.post("system", "Waiting for incoming connections...")
            else:
                self.post("system", "Waiting for incoming connections...")

            self.run_worker(self.accept_loop())
            self.run_worker(self.tunnel_watcher())
            self.run_worker(self.call_blink_worker())
            
            self.update_command_bar()
            
            
            if self.offline_ready():
                # Start Deaddrop raw SAM session
                await self.deaddrop.start()
                self.deaddrop_started = True

                # Deaddrop poller
                self.run_worker(self.poll_deaddrops())
                self.deaddrop_poller_started = True

                # Deaddrop connect test
                #asyncio.create_task(self.test_drop())
            
            
        except Exception as e:
            self.append_log_entry(f"[red]Initialization Error:[/] {e}")
            self.network_status = "initializing"
            
            try:
                self.query_one("#status_bar").update(Panel("[bold red]FAILED TO START SAM[/]", box=box.ROUNDED))
            except: pass
        
        

    async def on_input_submitted(self, event: Input.Submitted):
        # msg = event.value.strip()
        # if not msg: return
        # event.input.value = ""
        
        raw_msg = event.value
        msg = raw_msg.strip()
        if not msg:
            return

        if msg.startswith("/"):
            if not self.command_history or self.command_history[-1] != msg:
                self.command_history.append(msg)

        self.command_history_index = None
        self.command_history_current_buffer = ""
        event.input.value = ""

        if self.app_mode == "groups":
            if self.active_group:
                if msg.strip() in ("/profiles", "/contacts"):
                    self.open_profiles_screen()
                    return

                if msg.strip() == "/disconnect":
                    await self.close_group()
                    self.network_status = "visible"
                    self.peer_b32 = "Group Manager"
                    self.post("system", "Returned to group manager mode.")
                    return

                if msg.strip() == "/group":
                    self.open_active_group_screen()
                    return

                if msg.strip() == "/img":
                    self.open_file_picker(image_mode="braille")
                    return

                if msg.startswith("/img "):
                    path = msg[5:].strip()
                    if not path:
                        self.open_file_picker(image_mode="braille")
                        return
                    if not os.path.exists(path):
                        self.post("error", f"File not found: {path}")
                        return
                    await self.send_group_image(path, mode="braille")
                    return

                if msg.strip() == "/img-bw":
                    self.open_file_picker(image_mode="bw")
                    return

                if msg.startswith("/img-bw "):
                    path = msg[7:].strip()
                    if not path:
                        self.open_file_picker(image_mode="bw")
                        return
                    if not os.path.exists(path):
                        self.post("error", f"File not found: {path}")
                        return
                    await self.send_group_image(path, mode="bw")
                    return

                if msg.strip() in ("/log", "/logs"):
                    self.show_logs()
                    return

                if msg.strip() == "/help":
                    self.show_help()
                    return

                if msg.startswith("/"):
                    self.post("error", "Group chat supports /group, /profiles, /contacts, /img, /img-bw, /disconnect, /logs, and /help.")
                    return

                self.post("error", "Use the message composer above the command line to send chat text.")
                return

            if msg.strip() == "/admin":
                self.open_group_manager()
            elif msg.strip() in ("/profiles", "/contacts"):
                self.open_profiles_screen()
            elif msg.strip() in ("/log", "/logs"):
                self.show_logs()
            elif msg.strip() == "/help":
                self.show_help()
            else:
                self.post("error", "Group manager mode supports /admin, /profiles, /contacts, /logs, and /help.")
            return

        if self.app_mode == "group":
            if msg.strip() == "/disconnect":
                await self.close_group()
                self.exit()
                return

            if msg.strip() in ("/profiles", "/contacts"):
                self.open_profiles_screen()
                return

            if msg.strip() == "/group":
                self.open_active_group_screen()
                return

            if msg.strip() == "/img":
                self.open_file_picker(image_mode="braille")
                return

            if msg.startswith("/img "):
                path = msg[5:].strip()
                if not path:
                    self.open_file_picker(image_mode="braille")
                    return
                if not os.path.exists(path):
                    self.post("error", f"File not found: {path}")
                    return
                await self.send_group_image(path, mode="braille")
                return

            if msg.strip() == "/img-bw":
                self.open_file_picker(image_mode="bw")
                return

            if msg.startswith("/img-bw "):
                path = msg[7:].strip()
                if not path:
                    self.open_file_picker(image_mode="bw")
                    return
                if not os.path.exists(path):
                    self.post("error", f"File not found: {path}")
                    return
                await self.send_group_image(path, mode="bw")
                return

            if msg.strip() in ("/log", "/logs"):
                self.show_logs()
                return

            if msg.strip() == "/help":
                self.show_help()
                return

            if msg.startswith("/"):
                self.post("error", "This is group chat mode. Use /group, /profiles, /contacts, /img, /img-bw, /disconnect, /logs, or /help.")
                return

            if self.active_group:
                self.post("error", "Use the message composer above the command line to send chat text.")
            else:
                self.post("error", "Group is not ready.")
            return


        if msg.strip() == "/accept":
            await self.accept_pending_incoming()
            return

        if msg.strip() == "/decline":
            await self.decline_pending_incoming()
            return


        if msg.strip() == "/offline":
            if self.conn:
                self.post("error", "Cannot enter offline mode during active live chat.")
                return
            
            if self.offline_mode:
                self.post("error", "Already in OFFLINE mode.")
                return

            if not self.offline_ready():
                self.post("error", "Offline mode requires persistent mode with a locked peer.")
                return
            
            if not self.deaddrop_started or not self.deaddrop_poller_started:
                self.post("system", "Starting offline runtime...")
                await self.ensure_offline_runtime_started()
            

            self.offline_mode = True
            self.watch_peer_b32(self.peer_b32)
            self.update_command_bar()
            self.post("system", "Entered OFFLINE mode.")
            return


        if msg.strip() == "/online":
            if self.conn:
                self.post("error", "Already in live chat mode.")
                return

            if not self.offline_mode:
                self.post("error", "Already in normal standby mode.")
                return

            self.leave_offline_mode()
            self.update_command_bar()
            self.post("system", "Returned to normal standby mode.")
            return

        if msg.strip() == "/group":
            self.post("system", "Use --groups for group administration, or --group <group> to start group chat.")
            return

        if msg.strip() in ("/groups", "/group-list"):
            self.post("system", "Use --groups for group administration.")
            return

        if msg.strip() in ("/profiles", "/contacts"):
            self.open_profiles_screen()
            return

        if msg.startswith("/group"):
            self.post("system", "Use --groups for group management, or --group <group> for group chat.")
            return



        if msg.startswith("/connect"):
            if self.conn:
                self.post("error", "Already connected. Use /disconnect first.")
                return
            
            if self.pending_incoming_conn:
                self.post("error", "Incoming call is pending. Use /accept or /decline first.")
                return
            
            
            
            
            # if not self.is_persistent_mode() and not self.publish_ready:
            #     self.post("error", "Transient tunnels are still publishing. Wait a few seconds and try again.")
            #     return
            
            if not self.publish_ready:
                self.post("error", "Tunnels are still publishing. Wait a few seconds and try again.")
                return
            
            
            if self.offline_mode:
                self.leave_offline_mode()
                self.watch_peer_b32(self.peer_b32)
                self.post("system", "Leaving OFFLINE mode.")
            
            parts = msg.split(" ")
            if len(parts) > 1:
                # User provided address
                target = parts[1].strip()
                self.run_worker(self.connect_to_peer(target))
            elif self.stored_peer:
                # User typed /connect with no arguments
                self.post("system", f"Connecting to stored contact...")
                self.run_worker(self.connect_to_peer(self.stored_peer))
            else:
                self.post("error", "No stored contact. Use /connect <address>")
                
        
        
        
        elif msg.strip() == "/lock":
            if not self.is_persistent_mode():
                self.post("error", "Cannot lock in [bold green]TRANSIENT[/] mode. Restart with a profile name.")
                return
            
            if not self.conn:
                self.post("error", "No active connection to save.")
                return

            if self.stored_peer:
                self.post("error", f"Profile already locked to: {self.stored_peer}...")
                return

            if self.current_peer_addr:
               
                
                key_file = os.path.join(PROFILE_DIR, f"{self.profile}.dat")
                
                try:
                    if not self.current_peer_dest_b64:
                        self.post("error", "Peer full destination not yet known for TOFU pinning.")
                        return


                    with open(key_file, "r", encoding="utf-8") as f:
                        lines = [line.rstrip("\n") for line in f.readlines()]

                    if not lines:
                        raise RuntimeError("Identity file is empty")

                    content = lines[0] + "\n" + self.current_peer_addr + "\n" + self.current_peer_dest_b64 + "\n"
                    secure_write_text_atomic(key_file, content)
                    
                    

                    self.stored_peer = self.current_peer_addr
                    self.stored_peer_dest_b64 = self.current_peer_dest_b64
                    self.tofu_mismatch = False

                    # Initialize and persist offline state for this locked peer
                    self.drop_send_index = 0
                    self.drop_recv_base = 0
                    self.drop_window = 8
                    self.consumed_drop_recv = set()

                    self.save_offline_state()
                    
                    self.watch_peer_b32(self.peer_b32)

                    #await self.ensure_offline_runtime_started()

                    fp = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                    self.post("success", f"Profile {self.profile} is now locked to this peer.")
                    self.post("system", f"TOFU peer pin saved: {fp}")
                except Exception as e:
                    self.post("error", f"Failed to save: {e}")
            else:
                self.post("error", "Peer address not yet verified.")
         
         
        elif msg.strip() == "/unlock":
            if not self.is_persistent_mode():
                self.post("error", "Cannot unlock in [bold green]TRANSIENT[/] mode.")
                return

            if self.offline_mode:
                self.post("error", "Exit offline mode with /online before unlocking.")
                return

            if not self.stored_peer:
                self.post("error", "Profile is not locked to a peer.")
                return

            old_peer = self.stored_peer
            key_file = os.path.join(PROFILE_DIR, f"{self.profile}.dat")

            try:
                with open(key_file, "r", encoding="utf-8") as f:
                    lines = [line.rstrip("\n") for line in f.readlines()]

                if not lines:
                    raise RuntimeError("Identity file is empty")

                secure_write_text_atomic(key_file, lines[0] + "\n")

                safe_peer = old_peer.replace("/", "_")
                offline_path = os.path.join(PROFILE_DIR, f"offline_{safe_peer}.state")
                try:
                    if os.path.exists(offline_path):
                        os.remove(offline_path)
                except Exception as e:
                    self.post("status", f"Offline state cleanup failed: {e}")

                if self.offline_mode:
                    self.leave_offline_mode()

                self.stored_peer = None
                self.stored_peer_dest_b64 = None
                self.offline_shared_secret = b"CHANGE_ME_SHARED_OFFLINE_SECRET"
                self.drop_send_index = 0
                self.drop_recv_base = 0
                self.drop_window = 8
                self.consumed_drop_recv = set()
                self.seen_drop_msgs = set()
                self.dd_status = "idle"
                self.dd_status_ts = 0.0
                self.clear_tofu_runtime_status()
                self.watch_peer_b32(self.peer_b32)
                self.update_command_bar()

                self.post("success", f"Profile {self.profile} unlocked from stored peer.")
            except Exception as e:
                self.post("error", f"Failed to unlock profile: {e}")


        elif msg.startswith("/sendfile"):
            if not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return

            parts = msg.split(" ", 1)
            if len(parts) < 2:
                self.open_file_picker()
                return

            path = parts[1].strip()
            if not path:
                self.open_file_picker()
                return

            if not os.path.exists(path):
                self.post("error", "File not found.")
                return

            self.run_worker(self.send_file(path)) 
         
        
        
        elif msg.strip() == "/img":
            if not self.active_group and not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            self.open_file_picker(image_mode="braille")
            
        
        
        elif msg.startswith("/img "):
            if not self.active_group and not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            path = msg[5:].strip()
            if not path:
                self.open_file_picker(image_mode="braille")
                return

            if not os.path.exists(path):
                self.post("error", f"File not found: {path}")
                return

            if self.active_group:
                await self.send_group_image(path, mode="braille")
            else:
                await self.send_image(path, mode="braille")
            
            
        elif msg.strip() == "/img-bw":
            if not self.active_group and not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            self.open_file_picker(image_mode="bw")
            
            
        elif msg.startswith("/img-bw "):
            if not self.active_group and not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            path = msg[7:].strip()
            if not path:
                self.open_file_picker(image_mode="bw")
                return

            if not os.path.exists(path):
                self.post("error", f"File not found: {path}")
                return

            if self.active_group:
                await self.send_group_image(path, mode="bw")
            else:
                await self.send_image(path, mode="bw")
        
        
        
        elif msg.strip() == "/dd":
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            self.open_deaddrop_manager()
            return

        elif msg.strip() == "/dd-list":
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            self.show_deaddrop_servers()
            return

        elif msg.strip() == "/dd-add":
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            self.open_deaddrop_manager()
            return

        elif msg.startswith("/dd-add "):
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            server = msg[len("/dd-add "):].strip()
            if not server:
                self.open_deaddrop_manager()
                return
            self.open_deaddrop_manager(add_value=server)
            return

        elif msg.strip() == "/dd-del":
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            self.open_deaddrop_manager()
            return

        elif msg.startswith("/dd-del "):
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            arg = msg[len("/dd-del "):].strip()
            if not arg:
                self.open_deaddrop_manager()
                return
            try:
                idx = int(arg)
                self.open_deaddrop_manager(delete_index=idx)
            except ValueError:
                self.post("error", "Usage: /dd-del <number>")
            return

        elif msg.strip() == "/dd-share":
            if not self.is_persistent_mode():
                self.post("error", "Deaddrop commands are available only in PERSISTENT mode.")
                return
            if not self.conn:
                self.post("error", "No active connection to share deaddrop servers.")
                return
            await self.send_deaddrop_server_list()
            return
        
        
        
        elif msg.strip() == "/help":
            self.show_help()
            return

        elif msg.strip() == "/logs":
            self.show_logs()
            return

        elif msg.strip() == "/copyaddr":
            self.copy_my_addr_to_clipboard()
            return
        
                
        elif msg.strip() == "/disconnect":
            if self.pending_incoming_conn and not self.conn:
                self.post("error", "Incoming call is pending. Use /decline instead.")
                return
            self.run_worker(self.disconnect_peer())


        elif self.active_group:
            self.post("error", "Use the message composer above the command line to send chat text.")
            
            
        elif self.conn and self.live_ready:
            self.post("error", "Use the message composer above the command line to send chat text.")
                
        elif self.conn and not self.live_ready:
            self.post("error", "Live connection is not ready yet. Wait for secure session to be established.")
                
        elif self.offline_ready() and self.offline_mode:
            self.post("error", "Use the message composer above the command line to send chat text.")
                
                
        else:
            if self.is_persistent_mode() and not self.stored_peer:
                self.post("error", "Offline messaging requires a locked peer in PERSISTENT mode.")
            else:
                self.post("error", "No active connection. Use /connect <address>")
                
        
            
            
            

    async def connect_to_peer(self, target_address):
        current_task = asyncio.current_task()
        if current_task:
            self.sam_runtime.track_connect_task(current_task)
        try:
            if self.sam_runtime.is_closing():
                return
            
            self.current_peer_addr = target_address
            
            reader, writer = await self.sam_runtime.stream_connect(target_address)
            
            
            if hasattr(self, 'my_dest_b64'):
                # Send raw B64 address in single line
                writer.write(self.my_pub_dest_b64.encode() + b"\n")
                # Send 'S' frame to sync state machine
                writer.write(self.frame_message('S', self.my_pub_dest_b64))
                await writer.drain()
                
                # Send E2E key
                writer.write(self.frame_message('K', self.e2e.public_bytes()))
                await writer.drain()
                
                if self.pq_enabled:
                    writer.write(self.frame_message('Q', self.e2e.pq_public_bytes()))
                    await writer.drain()
                        
                
                self.proven = True 
                self.network_status = "visible" 
                self.watch_peer_b32(self.peer_b32) 


            self.conn = (reader, writer)
            self.live_ready = False
            
            self.watch_peer_b32(self.peer_b32)
            self.update_command_bar()
            
            self.post("success", "Handshake sent. Establishing tunnel...")
            self.run_worker(self.receive_loop(self.conn)) 
            
        
        except SamRuntimeClosed:
            self.conn = None
            self.live_ready = False
        except Exception as e:
            self.post("error", f"Connection failed: {e}")
            self.conn = None
            self.live_ready = False
            self.post("system", "Waiting for incoming connections...")
        





    async def accept_loop(self):
        current_task = asyncio.current_task()
        if current_task:
            self.sam_runtime.track_accept_task(current_task)
        while True:
            if self.sam_runtime.is_closing():
                return

            
            if self.conn or self.pending_incoming_conn:
                await asyncio.sleep(1)
                continue
            
            try:
                
                reader, writer = await self.sam_runtime.stream_accept()
                
                try:
                    peer_identity_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                except asyncio.TimeoutError:
                    writer.close()
                    continue
                
                
                if not peer_identity_line:
                    writer.close()
                    continue
                
                
                try:
                    raw_dest = peer_identity_line.decode().strip()
                    
                    peer_addr = self.sam.destination_to_b32(raw_dest)
                    
                    # If profile is LOCKED, verify calling peer b32
                    if self.stored_peer and peer_addr != self.stored_peer:
                        self.post("error", f"Blocked unauthorized call from {peer_addr}...")
                        writer.close()
                        continue
                    
                    
                    # TOFU check on b64 destination if pinned
                    if self.stored_peer_dest_b64 and raw_dest != self.stored_peer_dest_b64:
                        fp_old = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                        fp_new = self.peer_dest_fingerprint(raw_dest)
                        self.set_tofu_mismatch()
                        self.post("error", f"TOFU mismatch for {peer_addr}: expected {fp_old}, got {fp_new}")
                        writer.close()
                        continue
                 
                    
                    self.current_peer_addr = peer_addr
                    self.current_peer_dest_b64 = raw_dest
                    self.peer_b32 = peer_addr

                    if self.stored_peer_dest_b64:
                        self.set_tofu_verified()
                    else:
                        self.clear_tofu_runtime_status()

                    #self.post("success", f"Connection accepted from {peer_addr[:12]}...")
                except:
                    peer_addr = "Unknown"

                if self.pending_incoming_conn:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except:
                        pass
                    continue

                self.pending_incoming_conn = (reader, writer)
                self.pending_incoming_addr = self.current_peer_addr
                self.pending_incoming_dest_b64 = self.current_peer_dest_b64
                
                self.pending_incoming_task = asyncio.create_task(
                    self.pending_receive_loop(self.pending_incoming_conn)
                )
                
                self.watch_peer_b32(self.peer_b32)

                caller = self.pending_incoming_addr or "Unknown"
                self.post("system", f"Incoming call from {caller[:12]}... Type /accept or /decline.")
            
            except SamRuntimeClosed:
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                await asyncio.sleep(1)


    async def receive_loop(self, connection, initial_type=None):
        reader, writer = connection
        peer_addr = "Unknown"

        try:
            while True:

                # READ full frame. MAGIC based protocol
                try:
                    msg_type, msg_id, payload = await self.read_frame(reader)
                    if self.conn == connection:
                        self.mark_heartbeat_rx()
                    
                    # Decrypt payload if encrypted
                    if msg_type not in ('K','P','O','S','D','Z'):
                        payload = self.e2e.decrypt(payload)
                    
                except UnicodeDecodeError:
                # Stream not aligned. wait for next MAGIC
                    continue
                except ValueError:
                    # Invalid frame then resync
                    continue

                await self.handle_parsed_frame(msg_type, msg_id, payload, writer=writer, source="live")
                
                
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass

        except Exception as e:
            if self.conn == connection:
                self.post("error", f"Protocol Error: {e}")

        finally:
            if self.conn == connection:
                
                self.reset_transfer_state()
                self.stop_heartbeat()
                self.watch_peer_b32(self.peer_b32)
                
                self.conn = None
                self.live_ready = False
                self.pq_active = False
                self.current_peer_dest_b64 = None
                self.post("disconnect", "Peer disconnected.")
                self.peer_b32 = "Waiting for incoming connections..."
                self.clear_tofu_runtime_status()
                self.post("system", "Waiting for incoming connections...")
                
                self.update_command_bar()

            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass




    async def handle_parsed_frame(self, msg_type, msg_id, payload, writer=None, source="live"):
        body = payload.decode('utf-8', errors="ignore")

        if msg_type == 'U':
            bubble_type = "peer_offline" if source == "drop" else "peer"
            self.post(bubble_type, body)

            if writer is not None:
                writer.write(
                    self.frame_message(
                        'D',
                        struct.pack(">Q", msg_id)
                    )
                )
                await writer.drain()

        elif msg_type == 'D':
            delivered_id = struct.unpack(">Q", payload)[0]

            if delivered_id in self.pending_messages:
                entry = self.pending_messages.pop(delivered_id)
                self.mark_chat_entry_delivered(entry)

        elif msg_type == 'J':
            try:
                parts = body.split("|", 2)
                if len(parts) != 3:
                    self.post("error", "Invalid image header.")
                    return

                filename = os.path.basename(parts[0])[:MAX_FILENAME] or "image"
                mime = parts[1].strip()
                total = int(parts[2])

                if total <= 0 or total > MAX_FILE_SIZE:
                    self.post("error", f"Rejected image size: {total} bytes.")
                    return

                if not self.is_supported_image_mime(mime):
                    self.post("error", f"Unsupported incoming image type: {mime}")
                    return

                self.clear_incoming_image_state()
                self.incoming_image_name = filename
                self.incoming_image_mime = mime
                self.incoming_image_expected = total
                self.incoming_image_received = 0
                self.incoming_image_msg_id = msg_id
                self.incoming_image_bytes = bytearray()

            except Exception as e:
                self.post("error", f"Invalid image header: {e}")

        elif msg_type == 'G':
            try:
                if not self.incoming_image_name:
                    self.post("error", "Image chunk received without image header.")
                    return

                if self.incoming_image_msg_id != msg_id:
                    self.post("error", "Image chunk transfer id mismatch.")
                    return

                chunk = base64.b64decode(payload, validate=True)
                next_total = self.incoming_image_received + len(chunk)

                if next_total > self.incoming_image_expected:
                    self.post("error", "Image transfer overflow detected.")
                    self.clear_incoming_image_state()
                    return

                self.incoming_image_bytes.extend(chunk)
                self.incoming_image_received = next_total

            except Exception as e:
                self.post("error", f"Image chunk decode failed: {e}")
                self.clear_incoming_image_state()

        elif msg_type == 'Z':
            try:
                if not self.incoming_image_name:
                    self.post("error", "Image end received without image header.")
                    return

                if self.incoming_image_msg_id != msg_id:
                    self.post("error", "Image end transfer id mismatch.")
                    return

                if self.incoming_image_received != self.incoming_image_expected:
                    self.post(
                        "error",
                        f"Incomplete image transfer: {self.incoming_image_received}/{self.incoming_image_expected} bytes."
                    )
                    self.clear_incoming_image_state()
                    return

                image_mime = self.incoming_image_mime or "image/png"
                image_bytes = bytes(self.incoming_image_bytes)
                img_text = self.render_image_bytes_for_terminal(image_bytes, image_mime)

                self.append_chat_entry({
                    "kind": "image",
                    "content": img_text,
                    "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "display": "Peer",
                    "color": "cyan",
                    "alignment": "right",
                    "markup": True,
                })

                self.clear_incoming_image_state()

                if writer is not None:
                    writer.write(
                        self.frame_message(
                            'D',
                            struct.pack(">Q", msg_id)
                        )
                    )
                    await writer.drain()

            except Exception as e:
                self.post("error", f"Image receive failed: {e}")
                self.clear_incoming_image_state()

        elif msg_type == 'F':
            try:
                filename, size = body.split("|")
                filename = os.path.basename(filename)[:MAX_FILENAME]
                size = int(size)

                if size > MAX_FILE_SIZE:
                    self.post("error", f"File rejected (too large: {size} bytes)")
                    return

                safe_name = os.path.join(FILE_DIR, f"recv_{msg_id}_{filename}")

                self.incoming_file = open(safe_name, "wb")
                self.incoming_filename = filename
                self.incoming_expected = size
                self.incoming_received = 0
                self.rx_start_time = time.time()
                self.watch_peer_b32(self.peer_b32)

                self.post("system", f"Receiving file: {safe_name} ({size} bytes)")

            except Exception as e:
                self.post("error", f"Invalid file header: {e}")

        elif msg_type == 'C':
            try:
                if self.incoming_file:
                    chunk = base64.b64decode(payload)

                    self.incoming_received += len(chunk)
                    self.watch_peer_b32(self.peer_b32)

                    if self.incoming_received > self.incoming_expected:
                        self.post("error", "File transfer overflow detected")
                        self.incoming_file.close()
                        self.incoming_file = None
                        return

                    self.incoming_file.write(chunk)

            except Exception as e:
                self.post("error", f"File chunk error: {e}")

        elif msg_type == 'E':

            if self.incoming_file:
                self.incoming_file.close()

                self.post(
                    "success",
                    f"File received: {self.incoming_filename} ({self.incoming_received} bytes)"
                )

                self.incoming_file = None
                self.incoming_filename = None
                self.incoming_expected = 0
                self.incoming_received = 0
                self.rx_start_time = None
                self.watch_peer_b32(self.peer_b32)

        elif msg_type == 'S':

            if "__SIGNAL__:" in body:

                if body.startswith(HEARTBEAT_PING_PREFIX):
                    nonce = body[len(HEARTBEAT_PING_PREFIX):]
                    if writer is not None:
                        writer.write(
                            self.frame_message(
                                'S',
                                f"{HEARTBEAT_PONG_PREFIX}{nonce}"
                            )
                        )
                        await writer.drain()
                    return

                if body.startswith(HEARTBEAT_PONG_PREFIX):
                    return

                if body == OFFLINE_SECRET_REQUEST_SIGNAL:
                    self.post("system", "Peer requested offline secret sync.")
                    task = asyncio.create_task(self.send_offline_secret_if_needed())
                    self.sam_runtime.track_send_task(task)
                    return

                if "QUIT" in body:
                    if source == "pending":
                        caller = self.pending_incoming_addr or "Unknown"
                        
                        self.clear_pending_incoming()
                        self.current_peer_addr = None
                        self.current_peer_dest_b64 = None
                        self.peer_b32 = "Waiting for incoming connections..."
                        self.clear_tofu_runtime_status()

                        if self.pending_incoming_task:
                            self.pending_incoming_task.cancel()
                            self.pending_incoming_task = None

                        if writer is not None:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except:
                                pass

                        self.post("system", f"Incoming caller disconnected: {caller[:12]}...")
                        return

                    self.post("system", "Peer requested disconnect.")
                    return

            else:
                try:
                    
                    peer_addr = self.sam.destination_to_b32(body)

                    if self.stored_peer and peer_addr != self.stored_peer:
                        self.post("error", f"Locked peer mismatch: {peer_addr}")
                        if self.conn and writer is not None:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except:
                                pass
                        return

                    if self.stored_peer_dest_b64 and body != self.stored_peer_dest_b64:
                        fp_old = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                        fp_new = self.peer_dest_fingerprint(body)
                        self.set_tofu_mismatch()
                        self.post("error", f"TOFU mismatch for {peer_addr}: expected {fp_old}, got {fp_new}")
                        if self.conn and writer is not None:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except:
                                pass
                        return



                    self.current_peer_addr = peer_addr
                    self.current_peer_dest_b64 = body
                    self.peer_b32 = peer_addr
                    
                    if self.is_persistent_mode() and self.stored_peer_dest_b64:
                        self.set_tofu_verified()
                    else:
                        self.clear_tofu_runtime_status()

                    fp = self.peer_dest_fingerprint(body)

                    if self.is_persistent_mode():
                        self.post("info", f"Peer Identity: {peer_addr} (TOFU {fp})")
                    else:
                        self.post("info", f"Peer Identity: {peer_addr}")

                except:
                    pass

        elif msg_type == 'K':
            try:
                self.e2e.receive_peer_key(payload)
                
                if self.pq_enabled:
                    return
                
                if source == "pending":
                    return
                
                
                self.live_ready = True
                self.pq_active = False
                self.start_heartbeat()
                self.watch_peer_b32(self.peer_b32)
                
                self.update_command_bar()
                
                self.post("system", "Secure session established 🔐")
                
                if self.offline_ready():
                    task = asyncio.create_task(self.sync_offline_secret_if_needed())
                    self.sam_runtime.track_send_task(task)
                    
                if self.is_persistent_mode():
                    task = asyncio.create_task(self.send_deaddrop_server_list())
                    self.sam_runtime.track_send_task(task)
                
            except Exception as e:
                self.post("error", f"E2E key error: {e}")
                
                
                
        elif msg_type == 'Q':
            try:
                if not self.pq_enabled:
                    self.post("error", "Peer requires PQ, but this instance was started without --pq.")
                    if writer is not None:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except:
                            pass
                    return

                ciphertext = self.e2e.receive_peer_pq_public(payload)

                if writer is not None:
                    writer.write(self.frame_message('Y', ciphertext))
                    await writer.drain()

                self.mark_live_ready_if_needed()

            except Exception as e:
                self.post("error", f"PQ public key handling failed: {e}")
                


        elif msg_type == 'Y':
            try:
                if not self.pq_enabled:
                    self.post("error", "Unexpected PQ ciphertext while PQ is disabled.")
                    if writer is not None:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except:
                            pass
                    return

                self.e2e.receive_peer_pq_ciphertext(payload)
                self.mark_live_ready_if_needed()

            except Exception as e:
                self.post("error", f"PQ ciphertext handling failed: {e}")

                
                
        elif msg_type == 'X':
            try:
                if not self.offline_ready():
                    self.post("error", "Received offline secret outside persistent locked-peer mode.")
                    return

                if len(payload) != 32:
                    self.post("error", "Invalid offline secret length.")
                    return

                if self.has_real_offline_secret():
                    self.post("system", "Offline secret already exists. Ignoring replacement.")
                    return

                self.offline_shared_secret = payload
                self.save_offline_state()
                self.post("system", "Offline secret received and saved.")
            except Exception as e:
                self.post("error", f"Offline secret handling failed: {e}")
             
             
        elif msg_type == 'L':
            try:
                stripped = body.strip()
                if stripped.startswith("{"):
                    data = json.loads(stripped)
                    data_format = data.get("format")
                    data_kind = data.get("kind")
                    if data_format in ("icedcomm-i2p-group-roster", "icedcomm-i2p-group-invite"):
                        self.post("system", "Received group payload on direct chat; open a group session to process group rosters.")
                        return
                    if data_kind in (GROUP_CONTROL_JOIN_PROOF, GROUP_CONTROL_RENAME_REQUEST):
                        self.post("system", "Received group control payload on direct chat; ignoring outside group session.")
                        return

                servers = [line.strip() for line in body.splitlines() if line.strip()]
                if not servers:
                    return

                changed = self.merge_deaddrop_servers(servers)

                if changed:
                    self.post("system", f"Merged deaddrop server list from peer. Total: {len(self.deaddrop_servers)}")
                else:
                    self.post("system", "Received deaddrop server list from peer (no new entries).")
            except Exception as e:
                self.post("error", f"Failed to process deaddrop server list: {e}")     
             
             

        elif msg_type == 'P':
            if writer is not None:
                writer.write(self.frame_message('O', b''))
                await writer.drain()


    def show_group_list(self):
        groups = self.group_store.list_groups()
        if not groups:
            self.post("system", "No groups. Use /group-create <group name> | <your name> or /group-join <invite> <your name>.")
            return

        self.post("system", "Groups:")
        for meta in groups:
            key = group_storage_key(meta)
            status = "open" if self.active_group_key == key else "closed"
            member_count = len(meta.get("members") or [])
            self.post("system", f"{meta.get('name', 'group')} [{key[:12]}...] {member_count} members, {status}")


    async def on_mount_group_manager(self):
        self.network_status = "visible"
        self.peer_b32 = "Group Manager"
        self.post("system", f"{APP_NAME} {APP_VERSION}")
        self.post("system", "Mode: GROUP MANAGER")
        self.post("system", "No contact session is active.")
        self.show_group_list()
        self.open_group_manager()
        self.update_command_bar()


    async def on_mount_group_chat(self):
        self.network_status = "initializing"
        self.peer_b32 = "Initializing group..."
        self.post("system", f"{APP_NAME} {APP_VERSION}")
        self.post("system", "Mode: GROUP CHAT")
        self.post("system", f"Group selector: {self.group_selector}")
        await self.open_group(self.group_selector, acquire_lock=True)
        self.update_command_bar()


    def group_manager_rows(self) -> list[dict]:
        rows = []
        for index, meta in enumerate(self.group_store.list_groups(), start=1):
            key = group_storage_key(meta)
            owner = meta.get("owner_b32") or ""
            is_active = key == self.active_group_key
            is_locked = False if is_active else group_runtime_is_locked(self.group_store, key)
            rows.append({
                "index": index,
                "active": is_active,
                "state": "OPEN" if is_active else ("LOCKED" if is_locked else "READY"),
                "name": meta.get("name") or "group",
                "members": len(meta.get("members") or []),
                "owner": owner[:12] + "..." if owner else "",
                "key": key,
            })
        return rows


    def open_group_manager(self):
        if self.app_mode == "contact":
            self.post("system", "Use --groups to manage groups outside contact chat.")
            return

        if self.active_group:
            self.open_active_group_screen()
            return

        active_name = group_self_display_name(self.active_group) if self.active_group else ""

        self.push_screen(
            GroupManagerScreen(
                get_rows=self.group_manager_rows,
                open_group=self.open_group_from_manager,
                create_group=self.create_group_record,
                join_group=self.join_group_record,
                issue_invite=self.issue_group_invite_for_key,
                delete_group=self.delete_group_record,
                rename_me=lambda name: self.run_worker(self.rename_in_group(name)),
                active_group_key=self.active_group_key,
                active_group_open=self.active_group is not None,
                active_group_owner=group_is_admin(self.active_group) if self.active_group else False,
                active_display_name=active_name,
            )
        )


    def active_group_member_rows(self) -> list[dict]:
        if not self.active_group:
            return []

        rows = []
        my_b32 = (self.active_group.get("my_b32") or "").lower()
        owner_b32 = (self.active_group.get("owner_b32") or "").lower()

        if my_b32:
            rows.append({
                "role": "OWNER" if my_b32 == owner_b32 else "MEMBER",
                "state": "LOCAL",
                "name": group_self_display_name(self.active_group),
                "b32": my_b32,
            })

        for member in self.active_group.get("members") or []:
            normalized = normalize_member(member)
            b32 = normalized["b32"].lower()
            if b32 == my_b32:
                continue
            peer = self.group_peers.get(b32) or {}
            rows.append({
                "role": "OWNER" if b32 == owner_b32 else "MEMBER",
                "state": "READY" if peer.get("ready") and peer.get("authorized") else "OFFLINE",
                "name": normalized["name"],
                "b32": b32,
            })

        return rows


    def open_active_group_screen(self):
        if not self.active_group:
            self.post("error", "No group is open.")
            return

        self.push_screen(
            ActiveGroupScreen(
                group_name=self.active_group.get("name") or "group",
                is_owner=group_is_admin(self.active_group),
                get_rows=self.active_group_member_rows,
                rename_me=lambda name: self.run_worker(self.rename_in_group(name)),
                issue_invite=lambda: self.issue_group_invite_for_key(self.active_group_key),
                remove_member=lambda b32: self.run_worker(self.remove_group_member(b32)),
                display_name=group_self_display_name(self.active_group),
            )
        )


    def open_group_from_manager(self, key: str):
        if self.active_group:
            self.post("error", "Close the current group before opening another one.")
            return
        if group_runtime_is_locked(self.group_store, key):
            self.post("error", "Group is already open in another instance.")
            return
        self.run_worker(self.open_group(key, acquire_lock=True))


    def show_group_start_command(self, key: str):
        meta = self.group_store.find(key)
        if not meta:
            self.post("error", "Group not found.")
            return
        resolved_key = group_storage_key(meta)
        self.post("system", f"Start group chat with: {os.path.basename(sys.argv[0])} --group {resolved_key}")


    def create_group_record(self, group_name: str, my_name: str = ""):
        try:
            meta = make_group_meta(group_name, my_name)
            key = self.group_store.save(meta)
            self.post("success", f"Created group: {meta.get('name', 'group')}")
            self.post("system", f"Start once to initialize group identity: {os.path.basename(sys.argv[0])} --group {key}")
        except Exception as e:
            self.post("error", f"Group create failed: {e}")


    def join_group_record(self, invite_text: str, my_name: str):
        try:
            invite = decode_group_invite_string(invite_text)
            meta = make_group_meta(invite.get("group_name") or "group", my_name)
            merge_group_invite(meta, invite)
            key = group_storage_key(meta)
            existing = self.group_store.find(key)
            if existing and group_storage_key(existing).lower() == key.lower():
                self.post("error", "Group already exists locally. Open the existing group instead.")
                return
            if group_runtime_is_locked(self.group_store, key):
                self.post("error", "Group is already open in another instance.")
                return
            self.group_store.save(meta)
            self.post("success", f"Joined group record: {meta.get('name', 'group')}")
            self.post("system", f"Start group chat with: {os.path.basename(sys.argv[0])} --group {key}")
        except Exception as e:
            self.post("error", f"Group join failed: {e}")


    def delete_group_record(self, key: str):
        try:
            meta = self.group_store.find(key)
            if not meta:
                self.post("error", "Group not found.")
                return
            resolved_key = group_storage_key(meta)
            if self.active_group_key == resolved_key:
                self.post("error", "Cannot delete an active group.")
                return
            if group_runtime_is_locked(self.group_store, resolved_key):
                self.post("error", "Cannot delete group while it is open in another instance.")
                return
            lock = GroupRuntimeLock(self.group_store, resolved_key)
            try:
                lock.acquire()
            except Exception:
                self.post("error", "Cannot delete group while it is open in another instance.")
                return
            try:
                self.group_store.delete_key(resolved_key)
                self.post("success", f"Deleted group: {meta.get('name', resolved_key)}")
            finally:
                try:
                    lock.release()
                except:
                    pass
        except Exception as e:
            self.post("error", f"Group delete failed: {e}")


    def issue_group_invite_for_key(self, key: str | None = None) -> str | None:
        if key:
            meta = self.group_store.find(key)
        else:
            meta = self.active_group

        if not meta:
            self.post("error", "No group selected.")
            return None

        try:
            updated, invite = issue_group_invite(meta)
            if self.active_group and group_storage_key(self.active_group) == group_storage_key(updated):
                self.active_group = updated
                self.active_group_key = self.group_store.save(updated)
            else:
                self.group_store.save(updated)
            self.post("success", "Group invite:")
            try:
                pyperclip.copy(invite)
                self.post("success", "Group invite copied to system clipboard.")
            except Exception as e:
                self.post("error", f"Clipboard copy failed: {e}")
            self.post("system", invite)
            return invite
        except Exception as e:
            self.post("error", f"Group invite failed: {e}")
            return None


    def show_group_members(self):
        if not self.active_group:
            self.post("error", "No group is open. Use /group-open <name-or-key>.")
            return

        self.post("system", f"Group members for {self.active_group.get('name', 'group')}:")
        my_b32 = (self.active_group.get("my_b32") or "").lower()
        owner_b32 = (self.active_group.get("owner_b32") or "").lower()

        if my_b32:
            label = group_self_display_name(self.active_group)
            suffix = " owner" if my_b32 == owner_b32 else " me"
            self.post("system", f"{label}: {my_b32}{suffix}")

        for member in self.active_group.get("members") or []:
            b32 = (member.get("b32") or "").lower()
            if b32 == my_b32:
                continue
            peer = self.group_peers.get(b32) or {}
            state = "ready" if peer.get("ready") and peer.get("authorized") else "offline"
            suffix = " owner" if b32 == owner_b32 else ""
            self.post("system", f"{member.get('name', 'member')}: {b32} [{state}]{suffix}")


    async def create_group(self, group_name: str, my_name: str = ""):
        try:
            meta = make_group_meta(group_name, my_name)
            self.group_store.save(meta)
            await self.open_group(group_storage_key(meta))
        except Exception as e:
            self.post("error", f"Group create failed: {e}")


    async def join_group(self, invite_text: str, my_name: str):
        try:
            invite = decode_group_invite_string(invite_text)
            meta = make_group_meta(invite.get("group_name") or "group", my_name)
            merge_group_invite(meta, invite)
            key = group_storage_key(meta)
            existing = self.group_store.find(key)
            if existing and group_storage_key(existing).lower() == key.lower():
                self.post("error", "Group already exists locally. Open the existing group instead.")
                return
            if group_runtime_is_locked(self.group_store, key):
                self.post("error", "Group is already open in another instance.")
                return
            self.group_store.save(meta)
            await self.open_group(key)
        except Exception as e:
            self.post("error", f"Group join failed: {e}")


    async def open_group(self, selector: str, acquire_lock: bool = False):
        meta = self.group_store.find(selector)
        if not meta:
            self.post("error", "Group not found.")
            if acquire_lock:
                self.exit()
            return
        old_key = group_storage_key(meta)

        await self.close_group(quiet=True)

        try:
            if acquire_lock:
                self.group_runtime_lock = GroupRuntimeLock(self.group_store, old_key)
                self.group_runtime_lock.acquire()

            self.group_sam_runtime = SamSessionManager(self.sam_address[0], self.sam_address[1])
            self.group_sam = self.group_sam_runtime.client
            await self.group_sam_runtime.connect()

            if not meta.get("my_dest_b64"):
                self.post("system", "Generating group identity...")
                group_pub_dest_b64, group_dest_b64 = await self.group_sam_runtime.generate_destination(sig_type=7)
                meta["my_dest_b64"] = group_dest_b64
                self.group_pub_dest_b64 = group_pub_dest_b64

            session_b32 = str(meta.get("my_b32") or "").lower()
            if is_valid_b32_address(session_b32):
                self.group_session_id = f"chat_group_{session_b32[:6]}_{session_b32[-14:-8]}_{int(time.time())}"
            else:
                self.group_session_id = f"chat_group_init_{int(time.time())}"
            await self.group_sam_runtime.create_session(
                self.group_session_id,
                destination=meta["my_dest_b64"],
                options={
                    "inbound.length": "2",
                    "outbound.length": "2",
                    "inbound.quantity": "3",
                    "outbound.quantity": "3"
                }
            )

            self.group_pub_dest_b64 = await self.group_sam_runtime.naming_lookup("ME")
            my_b32 = self.group_sam.destination_to_b32(self.group_pub_dest_b64)
            meta["my_b32"] = my_b32

            if not meta.get("owner_b32"):
                meta["owner_b32"] = my_b32
                meta["id"] = my_b32

            if group_is_admin(meta):
                sign_group_roster_if_admin(meta)

            key = self.group_store.save(meta)
            if old_key != key:
                self.group_store.delete_key(old_key)
                if self.group_runtime_lock:
                    self.group_runtime_lock.release()
                    self.group_runtime_lock = GroupRuntimeLock(self.group_store, key)
                    self.group_runtime_lock.acquire()
            self.active_group_key = key
            self.active_group = meta
            self.group_peers = {}
            self.group_pending_messages = {}

            self.post("success", f"Opened group: {meta.get('name', 'group')}")
            self.post("system", f"Group address: {my_b32}")
            self.network_status = "local_ok"
            self.peer_b32 = f"Group: {meta.get('name', 'group')}"
            self.watch_peer_b32(self.peer_b32)

            self.group_accept_task = self.group_sam_runtime.track_accept_task(
                asyncio.create_task(self.group_accept_loop(key))
            )
            self.group_ready_task = asyncio.create_task(self.group_ready_loop(key, my_b32))
            self.update_command_bar()
        except Exception as e:
            await self.close_group(quiet=True)
            self.post("error", f"Group open failed: {e}")
            if acquire_lock:
                self.exit()


    async def close_group(self, quiet: bool = False):
        runtime = self.group_sam_runtime
        if runtime:
            runtime.begin_closing()

        if self.group_accept_task:
            self.group_accept_task.cancel()
            try:
                await self.group_accept_task
            except:
                pass
            self.group_accept_task = None

        if self.group_reconnect_task:
            self.group_reconnect_task.cancel()
            try:
                await self.group_reconnect_task
            except:
                pass
            self.group_reconnect_task = None

        if self.group_ready_task:
            self.group_ready_task.cancel()
            try:
                await self.group_ready_task
            except:
                pass
            self.group_ready_task = None

        known_writers = set()
        for peer in list(self.group_peers.values()):
            connect_task = peer.get("connect_task")
            if connect_task:
                connect_task.cancel()
                try:
                    await connect_task
                except:
                    pass
            task = peer.get("task")
            if task:
                task.cancel()
            heartbeat_task = peer.get("heartbeat_task")
            if heartbeat_task:
                heartbeat_task.cancel()
            handshake_timeout_task = peer.get("handshake_timeout_task")
            if handshake_timeout_task:
                handshake_timeout_task.cancel()
            writer = peer.get("writer")
            if writer is not None:
                known_writers.add(writer)
                try:
                    writer.write(self.frame_message("S", "__SIGNAL__:QUIT"))
                    await writer.drain()
                    await asyncio.sleep(0.03)
                except:
                    pass
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass

        if runtime:
            await runtime.wait_for_tasks()
            await runtime.close_registered_streams(exclude_writers=known_writers)
            await runtime.close_client_after_grace()
        elif self.group_sam:
            try:
                await self.group_sam.close()
            except:
                pass

        if self.group_runtime_lock:
            try:
                self.group_runtime_lock.release()
            except:
                pass
            self.group_runtime_lock = None

        closed_name = self.active_group.get("name", "group") if self.active_group else "group"
        self.active_group_key = None
        self.active_group = None
        self.group_sam_runtime = None
        self.group_sam = None
        self.group_session_id = None
        self.group_pub_dest_b64 = None
        self.group_publish_ready = False
        self.group_peers = {}
        self.group_pending_messages = {}
        if self.app_mode == "groups":
            self.network_status = "visible"
            self.peer_b32 = "Group Manager"
            self.watch_peer_b32(self.peer_b32)
        self.update_command_bar()

        if not quiet:
            self.post("system", f"Closed group: {closed_name}")


    def issue_active_group_invite(self):
        self.issue_group_invite_for_key(self.active_group_key)


    async def rename_in_group(self, name: str):
        if not self.active_group:
            self.post("error", "No group is open. Use /group-open <name-or-key>.")
            return
        try:
            name = validate_group_display_name(name)
        except Exception as e:
            self.post("error", str(e))
            return

        self.active_group["my_name"] = name
        if group_is_admin(self.active_group):
            sign_group_roster_if_admin(self.active_group)
            self.group_store.save(self.active_group)
            await self.send_group_roster_sync_to_ready_peers()
        else:
            self.group_store.save(self.active_group)
            await self.send_group_join_or_rename_to_owner()

        self.post("success", f"Group display name set to {self.active_group['my_name']}.")


    async def remove_group_member(self, member_b32: str):
        if not self.active_group:
            self.post("error", "No group is open.")
            return

        if not group_is_admin(self.active_group):
            self.post("error", "Only the group owner can remove members.")
            return

        member_b32 = (member_b32 or "").strip().lower()
        my_b32 = (self.active_group.get("my_b32") or "").lower()
        owner_b32 = (self.active_group.get("owner_b32") or "").lower()

        if not member_b32:
            self.post("error", "No member selected.")
            return

        if member_b32 == my_b32:
            self.post("error", "Cannot remove yourself from your own open group.")
            return

        if member_b32 == owner_b32:
            self.post("error", "Cannot remove the group owner.")
            return

        members = self.active_group.get("members") or []
        remaining = [
            member
            for member in members
            if (member.get("b32") or "").lower() != member_b32
        ]

        if len(remaining) == len(members):
            self.post("error", "Selected member is not in the roster.")
            return

        peer = self.group_peers.get(member_b32)
        if peer:
            connect_task = peer.get("connect_task")
            if connect_task:
                try:
                    connect_task.cancel()
                    await connect_task
                except:
                    pass
            writer = peer.get("writer")
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass
            task = peer.get("task")
            if task:
                try:
                    task.cancel()
                except:
                    pass
            heartbeat_task = peer.get("heartbeat_task")
            if heartbeat_task:
                try:
                    heartbeat_task.cancel()
                except:
                    pass
            self.group_peers.pop(member_b32, None)

        self.active_group["members"] = remaining
        self.active_group["roster_version"] = int(self.active_group.get("roster_version") or 1) + 1
        sign_group_roster_if_admin(self.active_group)
        self.group_store.save(self.active_group)
        await self.send_group_roster_sync_to_ready_peers()
        self.watch_peer_b32(self.peer_b32)
        self.post("success", f"Removed group member: {member_b32}")


    def group_member_by_b32(self, b32: str):
        if not self.active_group:
            return None
        b32_l = b32.lower()
        if (self.active_group.get("my_b32") or "").lower() == b32_l:
            return {"name": group_self_display_name(self.active_group), "b32": b32_l}
        for member in self.active_group.get("members") or []:
            if (member.get("b32") or "").lower() == b32_l:
                return normalize_member(member)
        return None


    def ensure_group_peer(self, member: dict, authorized: bool = True):
        normalized = normalize_member(member)
        b32 = normalized["b32"].lower()
        peer = self.group_peers.get(b32)
        if peer:
            peer["member"] = normalized
            peer["authorized"] = peer.get("authorized", False) or authorized
            return peer

        peer = {
            "member": normalized,
            "authorized": authorized,
            "ready": False,
            "e2e": E2E(pq_enabled=False),
            "reader": None,
            "writer": None,
            "connect_task": None,
            "task": None,
            "heartbeat_task": None,
            "handshake_timeout_task": None,
            "heartbeat_last_rx_ts": 0.0,
            "heartbeat_last_ping_ts": 0.0,
            "connecting": False,
            "last_connect_attempt_ts": 0.0,
            "last_connect_skip_log_ts": 0.0,
            "handshake_identity_received": False,
            "handshake_key_received": False,
            "incoming_image_name": None,
            "incoming_image_mime": None,
            "incoming_image_expected": 0,
            "incoming_image_received": 0,
            "incoming_image_msg_id": 0,
            "incoming_image_bytes": bytearray(),
        }
        self.group_peers[b32] = peer
        return peer


    def clear_group_peer_incoming_image_state(self, peer: dict):
        peer["incoming_image_name"] = None
        peer["incoming_image_mime"] = None
        peer["incoming_image_expected"] = 0
        peer["incoming_image_received"] = 0
        peer["incoming_image_msg_id"] = 0
        peer["incoming_image_bytes"] = bytearray()


    def group_local_prefers_outbound(self, peer_b32: str) -> bool:
        if not self.active_group:
            return False
        my_b32 = (self.active_group.get("my_b32") or "").lower()
        return bool(my_b32) and my_b32 < (peer_b32 or "").lower()


    async def close_group_writer(self, writer):
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass


    def cancel_group_peer_runtime_tasks(self, peer: dict):
        task = peer.get("task")
        if task:
            task.cancel()
            peer["task"] = None
        heartbeat_task = peer.get("heartbeat_task")
        if heartbeat_task:
            heartbeat_task.cancel()
            peer["heartbeat_task"] = None
        self.cancel_group_handshake_timeout(peer)


    def cancel_group_handshake_timeout(self, peer: dict):
        task = peer.get("handshake_timeout_task")
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        peer["handshake_timeout_task"] = None


    def start_group_handshake_timeout(self, peer_b32: str, writer):
        peer = self.group_peers.get(peer_b32)
        if not peer or peer.get("writer") is not writer:
            return
        self.cancel_group_handshake_timeout(peer)
        peer["handshake_timeout_task"] = asyncio.create_task(
            self.group_handshake_timeout_loop(peer_b32, writer)
        )


    async def group_handshake_timeout_loop(self, peer_b32: str, writer):
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(GROUP_HANDSHAKE_TIMEOUT)
            peer = self.group_peers.get(peer_b32)
            if not self.active_group or not peer:
                return
            if peer.get("writer") is not writer or peer.get("ready"):
                return

            peer["handshake_timeout_task"] = None
            self.post(
                "status",
                f"Group handshake timed out for {peer['member']['name']} ({peer_b32}); "
                f"identity_received={bool(peer.get('handshake_identity_received'))}, "
                f"key_received={bool(peer.get('handshake_key_received'))}. Closing stalled stream."
            )
            await self.close_group_writer(writer)
        except asyncio.CancelledError:
            pass
        finally:
            peer = self.group_peers.get(peer_b32)
            if peer and peer.get("handshake_timeout_task") is current_task:
                peer["handshake_timeout_task"] = None


    async def reconcile_group_peers(self):
        if not self.active_group:
            return

        my_b32 = (self.active_group.get("my_b32") or "").lower()
        members_by_b32 = {}
        for member in self.active_group.get("members") or []:
            normalized = normalize_member(member)
            b32 = normalized["b32"].lower()
            if b32 and b32 != my_b32:
                members_by_b32[b32] = normalized

        for peer_b32 in list(self.group_peers.keys()):
            if peer_b32 in members_by_b32:
                peer = self.group_peers[peer_b32]
                peer["member"] = members_by_b32[peer_b32]
                peer["authorized"] = True
                continue

            peer = self.group_peers.get(peer_b32)
            if peer and not peer.get("authorized"):
                continue

            peer = self.group_peers.pop(peer_b32)
            connect_task = peer.get("connect_task")
            if connect_task:
                connect_task.cancel()
                try:
                    await connect_task
                except:
                    pass
            writer = peer.get("writer")
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass
            task = peer.get("task")
            if task:
                task.cancel()
            heartbeat_task = peer.get("heartbeat_task")
            if heartbeat_task:
                heartbeat_task.cancel()
            handshake_timeout_task = peer.get("handshake_timeout_task")
            if handshake_timeout_task:
                handshake_timeout_task.cancel()

        for member in members_by_b32.values():
            self.ensure_group_peer(member, authorized=True)


    async def connect_group_members(self):
        if not self.active_group or not self.group_publish_ready:
            return
        if not self.group_sam_runtime or self.group_sam_runtime.is_closing():
            return

        await self.reconcile_group_peers()
        my_b32 = (self.active_group.get("my_b32") or "").lower()
        now = time.monotonic()
        for member in list(self.active_group.get("members") or []):
            normalized = normalize_member(member)
            if normalized["b32"].lower() == my_b32:
                continue
            peer = self.ensure_group_peer(normalized, authorized=True)
            if peer.get("ready"):
                continue
            if peer.get("connecting") or peer.get("writer"):
                if (
                    peer.get("writer")
                    and not peer.get("ready")
                    and now - peer.get("last_connect_skip_log_ts", 0.0) >= 10.0
                ):
                    peer["last_connect_skip_log_ts"] = now
                    self.post(
                        "status",
                        f"Group reconnect deferred for {peer['member']['name']}: "
                        f"connecting={bool(peer.get('connecting'))}, writer=True, ready=False."
                    )
                continue
            if (
                peer.get("last_connect_attempt_ts")
                and now - peer["last_connect_attempt_ts"] < GROUP_RECONNECT_INTERVAL
            ):
                continue
            peer["connecting"] = True
            peer["last_connect_attempt_ts"] = now
            task = asyncio.create_task(self.connect_group_peer(normalized["b32"]))
            peer["connect_task"] = self.group_sam_runtime.track_connect_task(task)


    async def connect_group_peer(self, b32: str):
        if not self.active_group or not self.group_sam_runtime:
            return
        if self.group_sam_runtime.is_closing():
            return
        peer_b32 = b32.lower()
        peer = self.group_peers.get(peer_b32)
        if not peer:
            return
        if peer.get("writer"):
            peer["connecting"] = False
            if peer.get("connect_task") == asyncio.current_task():
                peer["connect_task"] = None
            return

        try:
            self.post("system", f"Group outbound connect started: {peer['member']['name']} ({peer_b32}).")
            reader, writer = await self.group_sam_runtime.stream_connect(b32)
            self.post("system", f"Group outbound SAM stream connected: {peer['member']['name']} ({peer_b32}).")
            peer = self.group_peers.get(peer_b32)
            if not self.active_group or not peer:
                await self.close_group_writer(writer)
                return

            prefer_outbound = self.group_local_prefers_outbound(peer_b32)
            existing_writer = peer.get("writer")
            if existing_writer is not None:
                direction = "outbound" if prefer_outbound else "inbound"
                self.post(
                    "system",
                    f"Group collision decision for {peer['member']['name']}: "
                    f"local={self.active_group.get('my_b32', '').lower()}, peer={peer_b32}, "
                    f"preferred={direction}."
                )
            if peer.get("ready") and existing_writer is not None:
                peer["connecting"] = False
                await self.close_group_writer(writer)
                self.post("system", f"Group connection collision: kept ready session with {peer['member']['name']}.")
                return
            if existing_writer is not None and not prefer_outbound:
                peer["connecting"] = False
                await self.close_group_writer(writer)
                self.post("system", f"Group connection collision: kept inbound session with {peer['member']['name']}.")
                return

            old_writer = existing_writer
            if old_writer is not None:
                self.cancel_group_peer_runtime_tasks(peer)
                self.post("system", f"Group connection collision: kept outbound session with {peer['member']['name']}.")
            peer["reader"] = reader
            peer["writer"] = writer
            peer["connecting"] = False
            peer["ready"] = False
            peer["e2e"] = E2E(pq_enabled=False)
            peer["heartbeat_last_rx_ts"] = 0.0
            peer["heartbeat_last_ping_ts"] = 0.0
            peer["handshake_identity_received"] = False
            peer["handshake_key_received"] = False
            await self.send_group_handshake(writer, peer["e2e"])
            self.start_group_handshake_timeout(peer_b32, writer)
            peer["task"] = asyncio.create_task(self.group_receive_loop(peer_b32, reader, writer))
            if old_writer is not None:
                await self.close_group_writer(old_writer)
            self.post("system", f"Group handshake sent: {peer['member']['name']}")
        except asyncio.CancelledError:
            peer["connecting"] = False
            raise
        except SamRuntimeClosed:
            peer["connecting"] = False
        except Exception as e:
            self.post("status", f"Group connect failed for {peer['member']['name']}: {e}")
            peer["reader"] = None
            peer["writer"] = None
            peer["connecting"] = False
        finally:
            if peer and peer.get("connect_task") == asyncio.current_task():
                peer["connect_task"] = None


    async def group_reconnect_loop(self, group_key: str):
        try:
            while self.active_group_key == group_key and self.group_sam:
                await asyncio.sleep(1.0)
                await self.connect_group_members()
        except asyncio.CancelledError:
            pass


    async def group_ready_loop(self, group_key: str, my_b32: str):
        try:
            self.post("system", f"Waiting for group destination visibility: {my_b32}.")
            while self.active_group_key == group_key and self.group_sam_runtime:
                try:
                    if self.group_sam_runtime.is_closing():
                        return
                    await asyncio.wait_for(self.group_sam_runtime.naming_lookup(my_b32), timeout=5.0)
                    if self.active_group_key != group_key or not self.group_sam_runtime:
                        return
                    self.group_publish_ready = True
                    self.network_status = "visible"
                    self.watch_peer_b32(self.peer_b32)
                    self.post("success", "Group tunnels confirmed. Starting group member connections.")
                    self.group_reconnect_task = asyncio.create_task(self.group_reconnect_loop(group_key))
                    await self.connect_group_members()
                    return
                except asyncio.TimeoutError:
                    self.post("status", f"Group destination visibility lookup timed out: {my_b32}.")
                    await asyncio.sleep(2.0)
                except Exception as e:
                    self.post("status", f"Group destination visibility lookup failed: {type(e).__name__}: {e}")
                    await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass


    async def send_group_handshake(self, writer, e2e: E2E):
        if not self.group_pub_dest_b64:
            raise RuntimeError("group identity is not ready")
        writer.write(self.group_pub_dest_b64.encode() + b"\n")
        writer.write(self.frame_message("S", self.group_pub_dest_b64))
        writer.write(self.frame_message("K", e2e.public_bytes()))
        await writer.drain()


    async def group_accept_loop(self, group_key: str):
        while self.active_group_key == group_key and self.group_sam_runtime:
            try:
                if self.group_sam_runtime.is_closing():
                    return
                reader, writer = await self.group_sam_runtime.stream_accept()
                self.post("system", "Group SAM accept ready; waiting for an incoming caller identity.")
                try:
                    identity_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                except asyncio.TimeoutError:
                    self.post("status", "Group incoming accept timed out waiting for caller identity.")
                    writer.close()
                    continue
                if not identity_line:
                    self.post("status", "Group incoming accept closed before caller identity arrived.")
                    writer.close()
                    continue

                raw_dest = identity_line.decode().strip()
                peer_b32 = self.group_sam.destination_to_b32(raw_dest).lower()
                member = self.group_member_by_b32(peer_b32)
                authorized = member is not None
                self.post(
                    "system",
                    f"Group incoming caller identified: "
                    f"{member['name'] if member else 'unknown member'} ({peer_b32})."
                )

                if not authorized:
                    if not group_is_admin(self.active_group):
                        self.post("status", f"Rejected unauthorized group caller: {peer_b32}.")
                        writer.close()
                        await writer.wait_closed()
                        continue
                    member = {"name": f"member-{peer_b32[:8]}", "b32": peer_b32}

                peer = self.ensure_group_peer(member, authorized=authorized)
                prefer_outbound = self.group_local_prefers_outbound(peer_b32)
                existing_writer = peer.get("writer")
                has_collision = existing_writer is not None or peer.get("connecting")
                if has_collision:
                    direction = "outbound" if prefer_outbound else "inbound"
                    self.post(
                        "system",
                        f"Group collision decision for {peer['member']['name']}: "
                        f"local={self.active_group.get('my_b32', '').lower()}, peer={peer_b32}, "
                        f"preferred={direction}."
                    )
                if peer.get("ready") and existing_writer is not None:
                    await self.close_group_writer(writer)
                    self.post("system", f"Group connection collision: kept ready session with {peer['member']['name']}.")
                    continue
                if has_collision and prefer_outbound:
                    await self.close_group_writer(writer)
                    self.post("system", f"Group connection collision: kept outbound session with {peer['member']['name']}.")
                    continue

                old_writer = existing_writer
                if old_writer is not None:
                    self.cancel_group_peer_runtime_tasks(peer)
                    self.post("system", f"Group connection collision: kept inbound session with {peer['member']['name']}.")
                connect_task = peer.get("connect_task")
                if connect_task and not connect_task.done():
                    connect_task.cancel()
                peer["reader"] = reader
                peer["writer"] = writer
                peer["ready"] = False
                peer["e2e"] = E2E(pq_enabled=False)
                peer["connecting"] = False
                peer["heartbeat_last_rx_ts"] = 0.0
                peer["heartbeat_last_ping_ts"] = 0.0
                peer["handshake_identity_received"] = False
                peer["handshake_key_received"] = False
                await self.send_group_handshake(writer, peer["e2e"])
                self.start_group_handshake_timeout(peer_b32, writer)
                peer["task"] = asyncio.create_task(self.group_receive_loop(peer_b32, reader, writer))
                if old_writer is not None:
                    await self.close_group_writer(old_writer)
                self.post("system", f"Group incoming connection: {member['name']}")
            except asyncio.CancelledError:
                break
            except SamRuntimeClosed:
                break
            except Exception as e:
                self.post("status", f"Group accept failed: {type(e).__name__}: {e}")
                await asyncio.sleep(1)


    async def group_receive_loop(self, peer_b32: str, reader, writer):
        close_reason = "stream ended"
        try:
            while self.active_group and self.group_peers.get(peer_b32, {}).get("writer") == writer:
                try:
                    msg_type, msg_id, payload = await self.read_frame(reader)
                except UnicodeDecodeError as e:
                    self.post("status", f"Invalid group frame encoding from {peer_b32}: {e}")
                    continue
                except ValueError as e:
                    self.post("status", f"Invalid group frame from {peer_b32}: {e}")
                    continue
                await self.handle_group_frame(peer_b32, msg_type, msg_id, payload, writer)
        except asyncio.CancelledError:
            close_reason = "receive task cancelled"
            pass
        except asyncio.IncompleteReadError as e:
            close_reason = f"unexpected EOF ({len(e.partial)}/{e.expected} bytes)"
        except ConnectionResetError as e:
            close_reason = f"connection reset ({e})"
        except Exception as e:
            close_reason = f"{type(e).__name__}: {e}"
            peer = self.group_peers.get(peer_b32)
            if peer:
                self.post("status", f"Group protocol error from {peer['member']['name']}: {e}")
        finally:
            peer = self.group_peers.get(peer_b32)
            if peer and peer.get("writer") == writer:
                self.cancel_group_handshake_timeout(peer)
                if not peer.get("ready"):
                    self.post(
                        "status",
                        f"Group connection closed before secure handshake completed for "
                        f"{peer['member']['name']} ({peer_b32}): {close_reason}; "
                        f"identity_received={bool(peer.get('handshake_identity_received'))}, "
                        f"key_received={bool(peer.get('handshake_key_received'))}."
                    )
                peer["reader"] = None
                peer["writer"] = None
                peer["ready"] = False
                peer["connecting"] = False
                task = peer.get("heartbeat_task")
                if task:
                    task.cancel()
                    peer["heartbeat_task"] = None
                self.watch_peer_b32(self.peer_b32)
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass


    async def handle_group_frame(self, peer_b32: str, msg_type: str, msg_id: int, payload: bytes, writer):
        peer = self.group_peers.get(peer_b32)
        if not peer:
            return

        peer["heartbeat_last_rx_ts"] = time.monotonic()

        if msg_type == "S":
            body = payload.decode("utf-8", errors="ignore")
            if body.startswith(HEARTBEAT_PING_PREFIX):
                nonce = body[len(HEARTBEAT_PING_PREFIX):]
                writer.write(self.frame_message("S", f"{HEARTBEAT_PONG_PREFIX}{nonce}"))
                await writer.drain()
                return
            if body.startswith(HEARTBEAT_PONG_PREFIX):
                return
            if body == "__SIGNAL__:QUIT":
                writer.close()
                return

            try:
                announced_b32 = self.group_sam.destination_to_b32(body).lower()
                if announced_b32 != peer_b32.lower():
                    self.post("error", f"Group identity mismatch for {peer['member']['name']}: {announced_b32}")
                    writer.close()
                else:
                    peer["handshake_identity_received"] = True
                    self.post("system", f"Group identity verified: {peer['member']['name']}.")
            except Exception as e:
                self.post("status", f"Invalid group identity from {peer['member']['name']}: {e}")
            return

        if msg_type == "K":
            try:
                peer["e2e"].receive_peer_key(payload)
                peer["handshake_key_received"] = True
                if peer["e2e"].ready():
                    peer["ready"] = True
                    self.cancel_group_handshake_timeout(peer)
                    peer["heartbeat_last_rx_ts"] = time.monotonic()
                    peer["heartbeat_last_ping_ts"] = time.monotonic()
                    self.start_group_heartbeat(peer_b32)
                    self.post("system", f"Group secure session ready: {peer['member']['name']}")
                    self.network_status = "visible"
                    self.watch_peer_b32(self.peer_b32)
                    if group_is_admin(self.active_group):
                        await self.send_group_roster_sync(peer_b32)
                    else:
                        await self.send_group_join_or_rename_to_owner(peer_b32)
            except Exception as e:
                self.post("error", f"Group E2E key error from {peer['member']['name']}: {e}")
            return

        if msg_type == "D":
            if not peer.get("ready") or not peer.get("authorized") or len(payload) != 8:
                return
            delivered_id = struct.unpack(">Q", payload)[0]
            self.mark_group_message_delivered(delivered_id, peer_b32)
            return

        if msg_type in ("J", "G", "Z"):
            if not peer.get("ready") or not peer.get("authorized"):
                return

            if msg_type == "J":
                try:
                    plain = peer["e2e"].decrypt(payload)
                    parts = plain.decode("utf-8", errors="ignore").split("|", 2)
                    if len(parts) != 3:
                        self.post("error", f"Invalid group image header from {peer['member']['name']}.")
                        return

                    filename = os.path.basename(parts[0])[:MAX_FILENAME] or "image"
                    mime = parts[1].strip()
                    total = int(parts[2])

                    if total <= 0 or total > GROUP_IMAGE_TRANSFER_MAX_BYTES:
                        self.post("error", f"Rejected group image size from {peer['member']['name']}: {total} bytes.")
                        return

                    if not self.is_supported_image_mime(mime):
                        self.post("error", f"Unsupported group image type from {peer['member']['name']}: {mime}")
                        return

                    self.clear_group_peer_incoming_image_state(peer)
                    peer["incoming_image_name"] = filename
                    peer["incoming_image_mime"] = mime
                    peer["incoming_image_expected"] = total
                    peer["incoming_image_received"] = 0
                    peer["incoming_image_msg_id"] = msg_id
                    peer["incoming_image_bytes"] = bytearray()
                except Exception as e:
                    self.post("error", f"Invalid group image header from {peer['member']['name']}: {e}")
                return

            if msg_type == "G":
                try:
                    if not peer.get("incoming_image_name"):
                        self.post("error", f"Group image chunk without header from {peer['member']['name']}.")
                        return

                    if peer.get("incoming_image_msg_id") != msg_id:
                        self.post("error", f"Group image chunk transfer id mismatch from {peer['member']['name']}.")
                        return

                    plain = peer["e2e"].decrypt(payload)
                    chunk = base64.b64decode(plain, validate=True)
                    next_total = peer.get("incoming_image_received", 0) + len(chunk)

                    if next_total > peer.get("incoming_image_expected", 0) or next_total > GROUP_IMAGE_TRANSFER_MAX_BYTES:
                        self.post("error", f"Group image transfer overflow from {peer['member']['name']}.")
                        self.clear_group_peer_incoming_image_state(peer)
                        return

                    peer["incoming_image_bytes"].extend(chunk)
                    peer["incoming_image_received"] = next_total
                except Exception as e:
                    self.post("error", f"Group image chunk decode failed from {peer['member']['name']}: {e}")
                    self.clear_group_peer_incoming_image_state(peer)
                return

            try:
                if not peer.get("incoming_image_name"):
                    self.post("error", f"Group image end without header from {peer['member']['name']}.")
                    return

                if peer.get("incoming_image_msg_id") != msg_id:
                    self.post("error", f"Group image end transfer id mismatch from {peer['member']['name']}.")
                    return

                if peer.get("incoming_image_received") != peer.get("incoming_image_expected"):
                    self.post(
                        "error",
                        f"Incomplete group image from {peer['member']['name']}: "
                        f"{peer.get('incoming_image_received')}/{peer.get('incoming_image_expected')} bytes."
                    )
                    self.clear_group_peer_incoming_image_state(peer)
                    return

                image_mime = peer.get("incoming_image_mime") or "image/png"
                image_bytes = bytes(peer.get("incoming_image_bytes") or b"")
                img_text = self.render_image_bytes_for_terminal(image_bytes, image_mime)

                self.append_chat_entry({
                    "kind": "image",
                    "content": img_text,
                    "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "display": peer["member"].get("name", "member"),
                    "color": "cyan",
                    "alignment": "right",
                    "msg_id": msg_id,
                    "markup": True,
                })

                self.clear_group_peer_incoming_image_state(peer)
                writer.write(self.frame_message("D", struct.pack(">Q", msg_id)))
                await writer.drain()
            except Exception as e:
                self.post("error", f"Group image receive failed from {peer['member']['name']}: {e}")
                self.clear_group_peer_incoming_image_state(peer)
            return

        if msg_type not in ("U", "L"):
            return

        if not peer.get("ready"):
            return

        plain = peer["e2e"].decrypt(payload)

        if msg_type == "U":
            if not peer.get("authorized"):
                return
            body = plain.decode("utf-8", errors="ignore")
            self.append_chat_entry({
                "kind": "group_bubble",
                "mine": False,
                "group_name": self.active_group.get("name", "Group"),
                "author": peer["member"].get("name", "member"),
                "message": body,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "msg_id": msg_id,
            })
            writer.write(self.frame_message("D", struct.pack(">Q", msg_id)))
            await writer.drain()
            return

        await self.handle_group_list_payload(peer_b32, plain)


    async def handle_group_list_payload(self, peer_b32: str, plain: bytes):
        peer = self.group_peers.get(peer_b32)
        if not peer or not self.active_group:
            return

        try:
            data = json.loads(plain.decode("utf-8"))
        except Exception:
            return

        kind = data.get("kind")
        if kind == GROUP_CONTROL_JOIN_PROOF:
            if not group_is_admin(self.active_group):
                return
            if (data.get("b32") or "").lower() != peer_b32.lower():
                self.post("error", f"Rejected group invite proof b32 mismatch from {peer['member']['name']}.")
                return
            try:
                member = {"name": data.get("name") or f"member-{peer_b32[:8]}", "b32": data.get("b32")}
                redeem_group_invite_token(self.active_group, data.get("token") or "", member)
                peer["member"] = normalize_member(member)
                peer["authorized"] = True
                self.active_group_key = self.group_store.save(self.active_group)
                self.post("success", f"Redeemed group invite for {peer['member']['name']}.")
                await self.send_group_roster_sync_to_ready_peers()
                self.watch_peer_b32(self.peer_b32)
            except Exception as e:
                self.post("error", f"Rejected group invite proof from {peer['member']['name']}: {e}")
            return

        if kind == GROUP_CONTROL_RENAME_REQUEST:
            if not group_is_admin(self.active_group):
                return
            if (data.get("b32") or "").lower() != peer_b32.lower():
                self.post("error", f"Rejected group rename request b32 mismatch from {peer['member']['name']}.")
                return
            try:
                changed = apply_group_member_rename(self.active_group, peer_b32, data.get("name") or "")
                if changed:
                    self.active_group_key = self.group_store.save(self.active_group)
                    peer["member"] = normalize_member({"name": data.get("name"), "b32": peer_b32})
                    await self.send_group_roster_sync_to_ready_peers()
                    self.post("system", f"Accepted group rename request: {peer['member']['name']}.")
            except Exception as e:
                self.post("error", f"Rejected group rename request from {peer['member']['name']}: {e}")
            return

        data_format = data.get("format")
        if data_format == "icedcomm-i2p-group-roster":
            if not peer.get("authorized"):
                return
            try:
                merge_group_roster_sync(self.active_group, data)
                self.active_group_key = self.group_store.save(self.active_group)
                self.post("system", f"Merged group roster from {peer['member']['name']}.")
                await self.connect_group_members()
                self.watch_peer_b32(self.peer_b32)
            except Exception as e:
                self.post("error", f"Group roster sync failed from {peer['member']['name']}: {e}")
            return

        if data_format == "icedcomm-i2p-group-invite":
            if not peer.get("authorized"):
                return
            try:
                merge_group_invite(self.active_group, data)
                self.active_group_key = self.group_store.save(self.active_group)
                self.post("system", f"Merged legacy group roster from {peer['member']['name']}.")
                await self.connect_group_members()
                self.watch_peer_b32(self.peer_b32)
            except Exception as e:
                self.post("error", f"Group invite merge failed from {peer['member']['name']}: {e}")


    def start_group_heartbeat(self, peer_b32: str):
        peer = self.group_peers.get(peer_b32)
        if not peer:
            return
        task = peer.get("heartbeat_task")
        if task and not task.done():
            return
        peer["heartbeat_task"] = asyncio.create_task(self.group_heartbeat_loop(peer_b32))


    async def group_heartbeat_loop(self, peer_b32: str):
        try:
            while self.active_group and peer_b32 in self.group_peers:
                await asyncio.sleep(1.0)
                peer = self.group_peers.get(peer_b32)
                if not peer or not peer.get("ready"):
                    continue
                writer = peer.get("writer")
                if writer is None or writer.is_closing():
                    break

                now = time.monotonic()
                if not peer.get("heartbeat_last_rx_ts"):
                    peer["heartbeat_last_rx_ts"] = now
                if not peer.get("heartbeat_last_ping_ts"):
                    peer["heartbeat_last_ping_ts"] = now

                if now - peer["heartbeat_last_rx_ts"] >= HEARTBEAT_TIMEOUT:
                    self.post("system", f"Group member heartbeat timed out: {peer['member']['name']}")
                    writer.close()
                    break

                if (
                    now - peer["heartbeat_last_ping_ts"] >= HEARTBEAT_PING_INTERVAL
                    and now - peer["heartbeat_last_rx_ts"] >= HEARTBEAT_PING_INTERVAL
                ):
                    peer["heartbeat_last_ping_ts"] = now
                    writer.write(self.frame_message("S", f"{HEARTBEAT_PING_PREFIX}{self.heartbeat_nonce()}"))
                    await writer.drain()
        except asyncio.CancelledError:
            pass


    async def send_group_roster_sync(self, peer_b32: str):
        peer = self.group_peers.get(peer_b32)
        if not peer or not peer.get("ready") or not peer.get("authorized"):
            return
        try:
            roster = roster_sync_from_meta(self.active_group)
            payload = compact_json_bytes(roster)
            cipher = peer["e2e"].encrypt(payload)
            peer["writer"].write(self.frame_message("L", cipher))
            await peer["writer"].drain()
        except Exception as e:
            self.post("status", f"Group roster sync failed for {peer['member']['name']}: {e}")


    async def send_group_roster_sync_to_ready_peers(self):
        for peer_b32 in list(self.group_peers.keys()):
            await self.send_group_roster_sync(peer_b32)


    async def send_group_join_or_rename_to_owner(self, only_peer_b32: str | None = None):
        if not self.active_group:
            return
        owner_b32 = (self.active_group.get("owner_b32") or "").lower()
        my_b32 = self.active_group.get("my_b32")
        if not owner_b32 or not my_b32:
            return

        targets = [only_peer_b32.lower()] if only_peer_b32 else [owner_b32]
        for peer_b32 in targets:
            if peer_b32 != owner_b32:
                continue
            peer = self.group_peers.get(peer_b32)
            if not peer or not peer.get("ready") or not peer.get("writer"):
                continue

            token = self.active_group.get("join_token")
            if token:
                control = build_group_control(GROUP_CONTROL_JOIN_PROOF, token, my_b32, group_self_display_name(self.active_group))
            elif not group_is_admin(self.active_group) and self.active_group.get("my_name"):
                control = build_group_control(GROUP_CONTROL_RENAME_REQUEST, "", my_b32, group_self_display_name(self.active_group))
            else:
                continue

            cipher = peer["e2e"].encrypt(compact_json_bytes(control))
            peer["writer"].write(self.frame_message("L", cipher))
            await peer["writer"].drain()


    async def send_group_message(self, message: str):
        if self.group_sam_runtime and self.group_sam_runtime.is_closing():
            self.post("error", "Cannot send while group is closing.")
            return
        current_task = asyncio.current_task()
        if self.group_sam_runtime and current_task:
            self.group_sam_runtime.track_send_task(current_task)

        if not self.active_group:
            self.post("error", "No group is open.")
            return

        ready_peers = [
            (peer_b32, peer)
            for peer_b32, peer in self.group_peers.items()
            if peer.get("ready") and peer.get("authorized") and peer.get("writer")
        ]

        if not ready_peers:
            self.post("error", "No ready group members. Wait for group sessions to connect.")
            return

        msg_id = self.generate_msg_id()
        expected = [peer_b32 for peer_b32, _ in ready_peers]
        entry = {
            "kind": "group_bubble",
            "mine": True,
            "group_name": self.active_group.get("name", "Group"),
            "author": "Me",
            "message": message,
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "msg_id": msg_id,
            "group_expected_acks": expected,
            "group_received_acks": [],
        }
        self.group_pending_messages[msg_id] = entry

        sent_any = False
        for _, peer in ready_peers:
            try:
                cipher = peer["e2e"].encrypt(message.encode("utf-8"))
                peer["writer"].write(self.frame_message("U", cipher, msg_id=msg_id))
                await peer["writer"].drain()
                sent_any = True
            except Exception as e:
                self.post("status", f"Group send failed for {peer['member']['name']}: {e}")

        if sent_any:
            self.append_chat_entry(entry)
        else:
            self.group_pending_messages.pop(msg_id, None)
            self.post("error", "Group send failed.")


    async def send_group_image(self, path, mode="braille"):
        if self.group_sam_runtime and self.group_sam_runtime.is_closing():
            self.post("error", "Cannot send image while group is closing.")
            return
        current_task = asyncio.current_task()
        if self.group_sam_runtime and current_task:
            self.group_sam_runtime.track_send_task(current_task)

        if not self.active_group:
            self.post("error", "No group is open.")
            return

        ready_peers = [
            (peer_b32, peer)
            for peer_b32, peer in self.group_peers.items()
            if peer.get("ready") and peer.get("authorized") and peer.get("writer")
        ]

        if not ready_peers:
            self.post("error", "No ready group members. Wait for group sessions to connect.")
            return

        if self.image_mime_for_path(path) is None:
            self.post("error", "Unsupported image type.")
            return

        msg_id = None

        try:
            image_bytes, mime = self.prepare_image_preview_bytes(path)

            if not image_bytes:
                self.post("error", "Image preview is empty.")
                return

            if len(image_bytes) > GROUP_IMAGE_TRANSFER_MAX_BYTES:
                self.post(
                    "error",
                    f"Group image preview too large ({len(image_bytes)} bytes). "
                    f"Maximum is {GROUP_IMAGE_TRANSFER_MAX_BYTES} bytes."
                )
                return

            img_text = self.render_image_bytes_for_terminal(image_bytes, mime, mode=mode)
            filename = os.path.basename(path).replace("|", "_")[:MAX_FILENAME] or "image"
            msg_id = self.generate_msg_id()
            expected = [peer_b32 for peer_b32, _ in ready_peers]
            entry = {
                "kind": "image",
                "content": img_text,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "display": "Me",
                "color": "green",
                "alignment": "left",
                "msg_id": msg_id,
                "delivered": False,
                "markup": mode != "bw",
                "group_expected_acks": expected,
                "group_received_acks": [],
            }
            self.group_pending_messages[msg_id] = entry

            header = f"{filename}|{mime}|{len(image_bytes)}"
            sent_any = False
            for _, peer in ready_peers:
                try:
                    peer["writer"].write(
                        self.frame_message(
                            "J",
                            peer["e2e"].encrypt(header.encode("utf-8")),
                            msg_id=msg_id,
                        )
                    )

                    for start in range(0, len(image_bytes), 4096):
                        chunk = image_bytes[start:start + 4096]
                        encoded = base64.b64encode(chunk)
                        peer["writer"].write(
                            self.frame_message(
                                "G",
                                peer["e2e"].encrypt(encoded),
                                msg_id=msg_id,
                            )
                        )

                    peer["writer"].write(self.frame_message("Z", b"", msg_id=msg_id))
                    await peer["writer"].drain()
                    sent_any = True
                except Exception as e:
                    self.post("status", f"Group image send failed for {peer['member']['name']}: {e}")

            if sent_any:
                self.append_chat_entry(entry)
                self.post("success", f"Group image sent: {path}")
            else:
                self.group_pending_messages.pop(msg_id, None)
                self.post("error", "Group image send failed.")

        except Exception as e:
            if msg_id is not None:
                self.group_pending_messages.pop(msg_id, None)
            self.post("error", f"Group image send failed: {e}")


    def mark_group_message_delivered(self, delivered_id: int, peer_b32: str):
        entry = self.group_pending_messages.get(delivered_id)
        if not entry:
            return

        received = entry.setdefault("group_received_acks", [])
        if peer_b32 not in received:
            received.append(peer_b32)

        expected = entry.get("group_expected_acks") or []
        if expected and len(received) >= len(expected):
            self.group_pending_messages.pop(delivered_id, None)

        self.refresh_chat_entry(entry)


    async def tunnel_watcher(self):
        while True:
            if not hasattr(self, "my_b32"):
                await asyncio.sleep(2)
                continue

            try:
                await asyncio.wait_for(
                    self.sam.naming_lookup(self.my_b32),
                    timeout=5.0
                )

                if not self.publish_ready:
                    self.publish_ready = True
                    self.network_status = "visible"
                    self.post("success", "Tunnels confirmed. You can now initiate live connections.")

            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

            await asyncio.sleep(5)




    async def send_file(self, path):
        if self.sam_runtime and self.sam_runtime.is_closing():
            self.post("error", "Cannot send file while chat is closing.")
            return
        current_task = asyncio.current_task()
        if self.sam_runtime and current_task:
            self.sam_runtime.track_send_task(current_task)

        try:
            reader, writer = self.conn

            filename = os.path.basename(path)
            filesize = os.path.getsize(path)
            
            self.outgoing_file = True
            self.outgoing_filename = filename
            self.outgoing_total = filesize
            self.outgoing_sent = 0
            self.tx_start_time = time.time()
            
            if filesize > MAX_FILE_SIZE:
                self.post("error", f"File too large ({filesize} bytes)")
                return

            self.post("system", f"Sending file: {filename} ({filesize} bytes)")

            self.watch_peer_b32(self.peer_b32)
            
            header = f"{filename}|{filesize}"
            
            cipher = self.e2e.encrypt(header.encode())
            writer.write(self.frame_message('F', cipher))
            
            await writer.drain()

            with open(path, "rb") as f:
                while True:
                    chunk = f.read(4096)
                    
                    if not chunk:
                        break

                    self.outgoing_sent += len(chunk)
                    
                    

                    encoded = base64.b64encode(chunk).decode()
                    
                    cipher = self.e2e.encrypt(encoded.encode())
                    writer.write(self.frame_message('C', cipher))
                    
                    await writer.drain()
                    self.watch_peer_b32(self.peer_b32)

            # End transfer
            writer.write(self.frame_message('E', ''))
            await writer.drain()

            self.post("success", f"File sent: {filename}")
            
            self.outgoing_file = None
            self.tx_start_time = None
            
            self.watch_peer_b32(self.peer_b32)

        except Exception as e:
            self.reset_transfer_state()
            self.watch_peer_b32(self.peer_b32)
            self.post("error", f"File transfer failed: {e}")


    async def send_image(self, path, mode="braille"):
        if self.sam_runtime and self.sam_runtime.is_closing():
            self.post("error", "Cannot send image while chat is closing.")
            return
        current_task = asyncio.current_task()
        if self.sam_runtime and current_task:
            self.sam_runtime.track_send_task(current_task)

        if not self.conn:
            self.post("error", "No active connection")
            return

        reader, writer = self.conn

        if self.image_mime_for_path(path) is None:
            self.post("error", "Unsupported image type.")
            return

        msg_id = None

        try:
            image_bytes, mime = self.prepare_image_preview_bytes(path)

            if not image_bytes:
                self.post("error", "Image preview is empty.")
                return

            if len(image_bytes) > MAX_FILE_SIZE:
                self.post("error", f"Image preview too large ({len(image_bytes)} bytes).")
                return

            if mode == "bw":
                lines = render_bw(path, width=IMAGE_RENDER_WIDTH)
            else:
                lines = render_braille_color(path, width=IMAGE_RENDER_WIDTH)

            if len(lines) > MAX_IMAGE_LINES:
                self.post("error", "Image too large to render safely")
                return

            filename = os.path.basename(path).replace("|", "_")[:MAX_FILENAME] or "image"
            msg_id = self.generate_msg_id()
            img_text = "\n".join(lines)

            pending_entry = self.append_chat_entry({
                "kind": "image",
                "content": img_text,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "display": "Me",
                "color": "green",
                "alignment": "left",
                "msg_id": msg_id,
                "delivered": False,
                "markup": mode != "bw",
            })
            self.pending_messages[msg_id] = pending_entry

            header = f"{filename}|{mime}|{len(image_bytes)}"
            writer.write(
                self.frame_message(
                    'J',
                    self.e2e.encrypt(header.encode()),
                    msg_id=msg_id,
                )
            )

            for start in range(0, len(image_bytes), 4096):
                chunk = image_bytes[start:start + 4096]
                encoded = base64.b64encode(chunk)
                writer.write(
                    self.frame_message(
                        'G',
                        self.e2e.encrypt(encoded),
                        msg_id=msg_id,
                    )
                )

            writer.write(self.frame_message('Z', b'', msg_id=msg_id))
            await writer.drain()

            self.post("success", f"Image sent: {path}")

        except Exception as e:
            if msg_id is not None:
                self.pending_messages.pop(msg_id, None)
            self.post("error", f"Image send failed: {e}")


    async def send_control(self, signal: str):
        
        if self.conn:
            try:
                _, writer = self.conn
                
                writer.write(self.frame_message('S', f"__SIGNAL__:{signal}"))
                await writer.drain()
            except:
                pass




    async def disconnect_peer(self):
        if self.conn:
            reader, writer = self.conn
            
            self.reset_transfer_state()
            
            self.conn = None
            self.live_ready = False
            self.pq_active = False
            self.stop_heartbeat()
            self.current_peer_dest_b64 = None
            self.peer_b32 = "Waiting for incoming connections..."
            self.clear_tofu_runtime_status()
            self.watch_peer_b32(self.peer_b32)
            
            self.update_command_bar()
            
            try:
                
                writer.write(self.frame_message('S', "__SIGNAL__:QUIT"))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except:
                pass
            self.post("disconnect", "You disconnected.")
            self.post("system", "Waiting for incoming connections...")

 



    async def on_unmount(self):
        if self.app_mode in ("groups", "group"):
            try:
                await self.close_group(quiet=True)
            except:
                pass
            return
        
        try:
            self.save_offline_state()
        except:
            pass

        runtime = self.sam_runtime
        if runtime:
            runtime.begin_closing()
        
        known_writers = set()

        if self.pending_incoming_task:
            try:
                self.pending_incoming_task.cancel()
            except:
                pass
            self.pending_incoming_task = None
        
        
        if self.pending_incoming_conn:
            try:
                _, writer = self.pending_incoming_conn
                known_writers.add(writer)
                try:
                    writer.write(self.frame_message('S', "__SIGNAL__:QUIT"))
                    await writer.drain()
                    await asyncio.sleep(0.12)
                except:
                    pass
                writer.close()
                await writer.wait_closed()
            except:
                pass
            self.clear_pending_incoming()

        
        
        if self.conn:
            try:
                
                await self.send_control("QUIT")
                _, writer = self.conn
                known_writers.add(writer)
                await asyncio.sleep(0.12)
                writer.close()
                
                await writer.wait_closed()
                
            except:
                pass
            
        if runtime:
            await runtime.wait_for_tasks()
            await runtime.close_registered_streams(exclude_writers=known_writers)
            await runtime.close_client_after_grace()
        else:
            try:
                await self.sam.close()
            except:
                pass
        
        
        # Deaddrop SAM cleanup
        try:
            await self.deaddrop.close()
        except:
            pass



    def safe_filename(name):
        return os.path.basename(name)
    
    
    def reset_transfer_state(self):
        # Outgoing
        self.outgoing_file = None
        self.outgoing_filename = None
        self.outgoing_total = 0
        self.outgoing_sent = 0
        self.tx_start_time = None

        # Incoming
        if self.incoming_file:
            try:
                self.incoming_file.close()
            except:
                pass
        
        
        self.incoming_file = None
        self.incoming_filename = None
        self.incoming_expected = 0
        self.incoming_received = 0
        self.rx_start_time = None
    
    
    
    def get_file_transfer_status(self):
        # Will implement better status later on. Not critical.
        now = time.time()
        
        # Outgoing
        if self.outgoing_file and self.outgoing_total > 0:
            pct = int((self.outgoing_sent / self.outgoing_total) * 100)
            
            speed = 0
            if self.tx_start_time:
                elapsed = now - self.tx_start_time
                
                if elapsed < 1.0:
                    speed = 0
                else:
                    effective_time = max(elapsed, 0.5)

                    speed = int(self.outgoing_sent / effective_time / 1024)
                    
            name = self.outgoing_filename[:12]
            return f"[green]↑ {name} {pct}% {speed}KB/s[/]"

        # Incoming
        if self.incoming_file and self.incoming_expected > 0:
            pct = int((self.incoming_received / self.incoming_expected) * 100)
            
            speed = 0
            if self.rx_start_time:
                elapsed = now - self.rx_start_time
                
                if elapsed < 1.0:
                    speed =0
                else:
                    effective_time = max(elapsed, 0.5)

                    speed = int(self.incoming_received / effective_time / 1024)
                    
            name = os.path.basename(self.incoming_filename)[:12]
            return f"[cyan]↓ {name} {pct}% {speed}KB/s[/]"

        return None


    async def status_refresher(self):
        while True:
            self.watch_peer_b32(self.peer_b32)
            await asyncio.sleep(0.2)



    async def poll_deaddrops(self):
        await asyncio.sleep(2)  # let client fully start

        while True:
            try:
                if not self.offline_ready() or not self.offline_mode:
                    await asyncio.sleep(5)
                    continue
                
                
                if not hasattr(self, "my_b32"):
                    await asyncio.sleep(5)
                    continue

                if not self.get_offline_peer_b32():
                    await asyncio.sleep(5)
                    continue

                recv_window = self.get_deaddrop_recv_window()
                blob_key = self.get_offline_blob_key()
                self.set_dd_status("poll")

                for recv_index, dd_key in recv_window:
                    try:
                        blobs = await self.deaddrop.get(dd_key)

                        if not blobs:
                            self.set_dd_status("get_miss")
                            continue
                        
                        self.set_dd_status("get_hit")

                        got_valid_blob = False

                        
                        for drop, blob in blobs:
                            
                            self.prefer_deaddrop_server(drop)
                            
                            try:
                                blob_hash = hashlib.sha256(blob).hexdigest()

                                if blob_hash in self.seen_drop_msgs:
                                    continue

                                frame = self.e2e.decrypt_offline_blob(blob, blob_key)
                                msg_type, msg_id, payload = self.parse_frame_bytes(frame)

                                self.seen_drop_msgs.add(blob_hash)
                                got_valid_blob = True

                                await self.handle_parsed_frame(
                                    msg_type,
                                    msg_id,
                                    payload,
                                    writer=None,
                                    source="drop"
                                )

                                #self.post("system", f"[DROP] received type={msg_type} msg_id={msg_id} key_index={recv_index}")

                            except Exception as e:
                                self.post("error", f"[DROP parse error] {e}")

                        if got_valid_blob:
                            self.consumed_drop_recv.add(recv_index)
                            self.advance_drop_recv_base()
                            self.save_offline_state()

                    except Exception as e:
                        self.set_dd_status("get_fail")
                        self.post("error", f"[DROP key poll error] {e}")

            except Exception as e:
                self.set_dd_status("get_fail")
                self.post("error", f"[DROP polling error] {e}")

            await asyncio.sleep(5)



    async def test_drop(self):
        self.post("system", "Connecting to deaddrop...")
        await asyncio.sleep(5)

        self.post("system", "[TEST] starting deaddrop PUT")

        try:
            await self.deaddrop.put("test", b"hello_drop")
            self.post("success", "[TEST] PUT completed")
        except Exception as e:
            self.post("error", f"[TEST] PUT failed: {e}")



    def post_help_to_chat(self):
        for kind, text in HELP_LINES:
            self.post(kind, text)



    def show_help(self):
        try:
            self.push_screen(HelpScreen())
        except Exception:
            self.post_help_to_chat()





if __name__ == "__main__":
    app = None

    try:
        app = TermchatI2P()
        app.run()
    finally:
        if app is not None:
            try:
                app.flush_deaddrop_stats_if_needed(force=True)
            except Exception:
                pass

        if os.path.exists(BASE_DIR):
            try:
                remaining = fs_runtime_leave(BASE_DIR)

                if remaining == 0:
                    fs_encrypt(BASE_DIR, FS_PASSPHRASE)
                    print("[OK] Filesystem storage encrypted.")
            except Exception as e:
                print(f"[FS_ENCRYPT ERROR] Failed to encrypt storage: {e}")
