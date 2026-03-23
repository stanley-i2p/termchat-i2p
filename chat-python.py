# Python 3.10+ compatibility
# Use ./libi2p
import sys, os
import shutil
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import i2plib

import asyncio
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog
from textual.reactive import reactive
from textual.widgets import Static
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
from renderer import render_braille, render_bw
from PIL import Image

import struct
import random
import hashlib

from deaddrop import DeadDropClient
from e2e import E2E



MAGIC = b"\x89I2P"
PROTOCOL_VERSION = 3

# SECURITY LIMITS
MAX_FRAME_SIZE = 256 * 1024      # 256 KB max protocol frame
MAX_FILE_SIZE = 50 * 1024 * 1024 # 50 MB max file
MAX_IMAGE_LINES = 2000           # prevents huge ASCII images
MAX_FILENAME = 128

Image.MAX_IMAGE_PIXELS = 20_000_000



BASE_DIR = os.path.join(os.path.expanduser("~"), ".termchat-i2p")
BASE_DIR = os.path.abspath(BASE_DIR)

RESET_PROFILE = False

if len(sys.argv) > 2 and sys.argv[1] == "--reset":
    RESET_PROFILE = True
    PROFILE_NAME = os.path.basename(sys.argv[2])
elif len(sys.argv) > 1:
    PROFILE_NAME = os.path.basename(sys.argv[1])
else:
    PROFILE_NAME = "default"

PROFILE_DIR = os.path.join(BASE_DIR, "profiles", PROFILE_NAME)

IMAGE_DIR = os.path.join(BASE_DIR, "images")
FILE_DIR = os.path.join(BASE_DIR, "files")
BLOB_DIR = os.path.join(BASE_DIR, "blobs")

if RESET_PROFILE and os.path.exists(PROFILE_DIR):
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)
os.makedirs(BLOB_DIR, exist_ok=True)


class I2PChat(App):
    # This maps "q" or "ctrl+q" to the action "quit"
    BINDINGS = [("q", "quit", "Quit"), ("ctrl+q", "quit", "Quit"), ("c", "copy_my_addr", "Copy My B32")]
    
    CSS = """
    RichLog { height: 1fr; border: solid white; background: $surface; } 
    Input { dock: bottom; }
    #status_bar {
        dock: top;
        height: 3; /* Increased height to fit the box border */
        margin: 0 0;
        content-align: center middle;
        background: $surface; 
        color: $text;              
    }
    """

    
    peer_b32 = reactive("Waiting for incoming connections...")
    network_status = reactive("initializing") 

    def __init__(self):
        super().__init__()
        self.sam_address = ('127.0.0.1', 7656)
        self.sock = None  # LISTENER
        self.conn = None  # ACTIVE CHAT
        
        # New i2plib requirement for GC
        self.sam_reader = None
        self.sam_writer = None
        
        self.stored_peer = None
        self.stored_peer_dest_b64 = None
        self.current_peer_addr = None
        self.current_peer_dest_b64 = None
        
        self.tofu_verified = False
        self.tofu_mismatch = False
        
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
        
        
        self.image_buffer = []
        
        self.pending_messages = {}
        
        self.e2e = E2E()
        
        # Dеaddrop (Phase 1)
        self.deaddrop = DeadDropClient(
            self.dd_session_id,
            ["62afc5yf2lcthx44okvavvmvgb55cee3weqeqhuapcclz6evwyrq.b32.i2p",
             "x75crc4lkcd3xcfrj5sox662mujngzrtmvmejaixutdozg35fgvq.b32.i2p", "xxbgj3dlw7fvwz3emqnvyzxrdj3vqd3fcdw6rutmvzoxidyhp7bq.b32.i2p"
            ]
        )
        

        
        self.deaddrop_enabled = self.is_persistent_mode()
        self.deaddrop_started = False
        self.deaddrop_poller_started = False
        self.offline_mode = False
        
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
        yield RichLog(id="chat_window", highlight=False, markup=True)
        yield Input(placeholder="Type message and press Enter...")


    def watch_network_status(self, _):
        # Refresh panel when network status changes
        self.watch_peer_b32(self.peer_b32)

    
    def watch_peer_b32(self, new_val: str) -> None:
        
        status_map = {
            "initializing": ("[grey62]●[/]", "INITIALIZING", "grey62"),
            "local_ok": ("[yellow]●[/]", "BUILDING TUNNELS", "yellow"),
            "visible": ("[green]●[/]", "VISIBLE / READY", "green")
        }
    
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
        
        if self.tofu_mismatch:
            tofu_tag = " [black on red] TOFU [/]"
        elif self.tofu_verified:
            tofu_tag = " [black on green] TOFU [/]"
        else:
            tofu_tag = ""
        
        offline_tag = " [black on yellow] OFF [/]" if self.offline_mode else ""
        left_content = f"[black on {tag_bg}] [bold]{mode_tag}[/] [/] [bold]{self.profile.upper()}[/]{lock_tag}{tofu_tag}{offline_tag}"
        
        transfer = self.get_file_transfer_status()

        if transfer:
            conn_viz = transfer

        elif is_active:
            link_color = "green" if is_proven else "cyan"
            link_symbol = "●" if is_proven else "o"
            conn_viz = f"[bold {link_color}]{link_symbol}[/] [dim]CONNECTED[/]"
        else:
            
            conn_viz = f"[dim]{dot} [dim]STANDBY[/]"

        
        if hasattr(self, 'my_dest'):
            full_addr = self.my_dest.base32
            
            my_b32 = f"{full_addr[:6]}...{full_addr[-6:]}"
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

        
    
    

    def action_copy_my_addr(self) -> None:
        if hasattr(self, 'my_dest'):
            addr = self.my_dest.base32 + ".b32.i2p"
            pyperclip.copy(addr)  # Use xclip automatically
            self.post("success", "Copied to system clipboard!")


     

    def post(self, type_name: str, message: str):
        
        styles = {
            "info": "[bold blue]STATUS:[/] [white]{}[/]",
            "error": "[bold red]ERROR:[/] [red]{}[/]",
            #"system": "[bold yellow]SYSTEM:[/] [italic gray]{}[/]",
            "system": "[#878700]SYSTEM:[/] [dim #9f9f9f italic]{}[/]",
            "me": "[bold green]Me:[/] [white]{}[/]",
            "me_offline": "[bold yellow]Me-Offline:[/] [white]{}[/]",
            "peer_offline": "[bold magenta]Peer-Offline:[/] [white]{}[/]",
            "peer": "[bold cyan]Peer:[/] [white]{}[/]",
            "success": "[bold green]✔[/] [white]{}[/]",
            "disconnect": "[bold red]X[/] [white]{}[/]",
            "help": "[dim]HELP:[/] [gray62]{}[/]"
        }
        
        
        safe_message = re.sub(r'[\x00-\x1F\x7F]', '', str(message))
        safe_message = escape(safe_message)
        
        
        address_pattern = r"([a-z0-9]+\.b32\.i2p|[a-z0-9]+\.i2p)"
        formatted_msg = re.sub(address_pattern, r"[bold cyan]\1[/]", safe_message)

        
        content = styles.get(type_name, "{}").format(formatted_msg)
        
        
        
        if type_name in ["me", "peer", "me_offline", "peer_offline"]:
            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
            
            
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
            
            
            message_panel = Panel(
                f"[white]{formatted_msg}[/]",
                title=f"[#5f5f5f][{now_utc} UTC][/] [bold {box_color}]{display_name}[/]",
                title_align="left",
                border_style=box_color,
                box=box.ROUNDED,
                expand=False
            )
            
            self.chat_log.write(Align(message_panel, align=alignment), expand=True)

        else:
            self.chat_log.write(content)
            
            
    
    def frame_message(self, msg_type: str, payload):

        if isinstance(payload, str):
            payload = payload.encode()

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
        
        if msg_type not in b"UDISFCEKPOX":
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

        if msg_type not in b"UDISFCEKPOX":
            raise ValueError("Unknown frame type")

        if length < 0 or length > MAX_FRAME_SIZE:
            raise ValueError("Invalid frame size")

        if len(frame) != 18 + length:
            raise ValueError("Frame length mismatch")

        payload = frame[18:18 + length]

        return msg_type.decode(), msg_id, payload
    
    
    
    def generate_msg_id(self):
        return (int(time.time() * 1000) ^ random.getrandbits(32)) & 0xFFFFFFFFFFFFFFFF


    def peer_dest_fingerprint(self, dest_b64: str) -> str:
        return hashlib.sha256(dest_b64.encode()).hexdigest()[:16]


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




    def get_offline_peer_b32(self):
        peer = self.stored_peer or self.current_peer_addr
        if not peer:
            return None
        return peer.replace(".b32.i2p", "").strip().lower()


    def derive_deaddrop_key(self, direction: str, index: int) -> str:
        if not hasattr(self, "my_dest"):
            raise RuntimeError("Local destination not ready")

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            raise RuntimeError("Peer address not known for deaddrop key derivation")

        my_b32 = self.my_dest.base32.strip().lower()

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
        if not hasattr(self, "my_dest"):
            raise RuntimeError("Local destination not ready")

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            raise RuntimeError("Peer address not known for offline blob key")

        my_b32 = self.my_dest.base32.strip().lower()

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

            with open(path, "w") as f:
                f.write(f"offline_shared_secret={self.offline_shared_secret.hex()}\n")
                f.write(f"drop_send_index={self.drop_send_index}\n")
                f.write(f"drop_recv_base={self.drop_recv_base}\n")
                f.write(f"drop_window={self.drop_window}\n")
                f.write(
                    "consumed_drop_recv="
                    + ",".join(str(x) for x in sorted(self.consumed_drop_recv))
                    + "\n"
                )

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
        if not hasattr(self, "my_dest"):
            return False

        peer_b32 = self.get_offline_peer_b32()
        if not peer_b32:
            return False

        my_b32 = self.my_dest.base32.strip().lower()
        peer_b32 = peer_b32.strip().lower()

        return my_b32 < peer_b32


    async def send_offline_secret_if_needed(self):
        if not self.conn:
            return

        if not self.offline_ready():
            return

        if self.has_real_offline_secret():
            return
        
        if not self.should_initiate_offline_secret():
            return

        try:
            _, writer = self.conn

            self.offline_shared_secret = self.generate_offline_shared_secret()
            self.save_offline_state()

            writer.write(self.frame_message('X', self.offline_shared_secret))
            await writer.drain()

            self.post("system", "Offline secret sent to locked peer.")
        except Exception as e:
            self.post("error", f"Failed to send offline secret: {e}")



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




    async def on_mount(self):
        
        self.chat_log = self.query_one("#chat_window", RichLog)
        
        self.network_status = "initializing"
        
        self.peer_b32 = "Initializing SAM Session..."
        
        
        
        self.post("system", "Initializing SAM Session...")
        self.post("system", f"Initializing Profile: {self.profile}")
        
        if RESET_PROFILE:
            self.post("system", f"Profile {self.profile} was reset before startup.")
        
        
        
        #is_persistent = len(sys.argv) > 1
        is_persistent = self.profile != "default"
        
        
        self.chat_log.write(f"[#878700]SYSTEM:[/] [dim #5f5f5f italic]Mode:[/][not bold {'yellow' if is_persistent else 'green'}] {'PERSISTENT' if is_persistent else 'TRANSIENT'}[/]")

        
        
        key_file = os.path.join(PROFILE_DIR, f"{self.profile}.dat")

        try:
            
            dest = None
            # Handle Persistence
            if is_persistent and os.path.exists(key_file):
                with open(key_file, "r") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]
                    
                if len(lines) > 0:
                    raw_private_key = lines[0]
                    dest = i2plib.Destination(raw_private_key, has_private_key=True)
                    self.post("system", f"Loaded identity from {key_file}")
                
                
                if len(lines) > 1:
                    self.stored_peer = lines[1]
                    self.post("system", f"Locked Peer: {self.stored_peer}")
                    
                    
                if len(lines) > 2:
                    self.stored_peer_dest_b64 = lines[2]
                    fp = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                    self.post("system", f"TOFU peer pin loaded: {fp}")
                    
                    
                    
            # Generate new
            if dest is None:
                self.post("system", "Generating new Ed25519 identity...")
                dest = await i2plib.new_destination(sam_address=self.sam_address, sig_type=7)
                
                
                if is_persistent:
                    with open(key_file, "w") as f:
                        f.write(dest.private_key.base64 + "\n")
                        
                    self.post("success", f"Identity saved to [cyan]{key_file}[/]")
                
                
            self.my_dest = dest
            
            # Create SAM socket
            # New i2plib compatibility.
            self.sam_reader, self.sam_writer = await i2plib.create_session(
                self.session_id, 
                destination=self.my_dest, 
                sam_address=self.sam_address,
                options={
                    "inbound.length": "2",
                    "outbound.length": "2",
                    "inbound.quantity": "3",
                    "outbound.quantity": "3"
                }
            )
            
            
            

            self.network_status = "local_ok"
            my_address = self.my_dest.base32 + ".b32.i2p"
            self.post("success", f"Online! My Address: {my_address}")
            
            self.peer_b32 = f"My Addr: {my_address}"
            
            if self.stored_peer:
                
                self.load_offline_state()
                
                self.post("system", "Type /connect to dial stored contact.")
                self.post("system", "Waiting for incoming connections...")
            else:
                self.post("system", "Waiting for incoming connections...")

            self.run_worker(self.accept_loop())
            
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
            self.chat_log.write(f"[red]Initialization Error:[/] {e}")
            self.network_status = "initializing"
            
            try:
                self.query_one("#status_bar").update(Panel("[bold red]FAILED TO START SAM[/]", box=box.ROUNDED))
            except: pass
        
        

    async def on_input_submitted(self, event: Input.Submitted):
        msg = event.value.strip()
        if not msg: return
        event.input.value = ""


        if msg.strip() == "/offline":
            if self.conn:
                self.post("error", "Cannot enter offline mode during active live chat.")
                return

            if not self.offline_ready():
                self.post("error", "Offline mode requires persistent mode with a locked peer.")
                return

            self.offline_mode = True
            self.watch_peer_b32(self.peer_b32)
            self.post("system", "Entered OFFLINE mode.")
            return


        if msg.startswith("/connect"):
            if self.conn:
                self.post("error", "Already connected. Use /disconnect first.")
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

                    with open(key_file, "a") as f:
                        f.write(self.current_peer_addr + "\n")
                        f.write(self.current_peer_dest_b64 + "\n")

                    self.stored_peer = self.current_peer_addr
                    self.stored_peer_dest_b64 = self.current_peer_dest_b64
                    self.tofu_mismatch = False

                    # Initialize and persist offline state for this locked peer
                    self.drop_send_index = 0
                    self.drop_recv_base = 0
                    self.drop_window = 8
                    self.consumed_drop_recv = set()

                    self.save_offline_state()

                    await self.ensure_offline_runtime_started()

                    fp = self.peer_dest_fingerprint(self.stored_peer_dest_b64)
                    self.post("success", f"Profile [bold yellow]{self.profile}[/] is now locked to this peer.")
                    self.post("system", f"TOFU peer pin saved: [cyan]{fp}[/]")
                except Exception as e:
                    self.post("error", f"Failed to save: {e}")
            else:
                self.post("error", "Peer address not yet verified.")
         
         
        elif msg.startswith("/sendfile"):
            if not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return

            parts = msg.split(" ", 1)
            if len(parts) < 2:
                self.post("error", "Usage: /sendfile <path>")
                return

            path = parts[1].strip()

            if not os.path.exists(path):
                self.post("error", "File not found.")
                return

            self.run_worker(self.send_file(path)) 
         
        
        
        elif msg.startswith("/img "):
            if not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            path = msg[5:].strip()

            if not os.path.exists(path):
                self.post("error", f"File not found: {path}")
                return

            await self.send_image(path, mode="braille")
            
            
        elif msg.startswith("/img-bw "):
            if not self.conn:
                self.post("error", "No active connection. Use /connect <address>.")
                return
            
            path = msg[7:].strip()

            if not os.path.exists(path):
                self.post("error", f"File not found: {path}")
                return

            await self.send_image(path, mode="bw")
        
        
        elif msg.strip() == "/help":
            self.show_help()
            return
        
                
        elif msg.strip() == "/disconnect":
            self.run_worker(self.disconnect_peer())
        elif self.conn:
            try:
                _, writer = self.conn
                
                cipher = self.e2e.encrypt(msg.encode())
                frame = self.frame_message('U', cipher)
                msg_id = struct.unpack(">Q", frame[6:14])[0]
                
                self.pending_messages[msg_id] = msg

                writer.write(frame)
                await writer.drain()
                

                self.post("me", msg)
            except Exception:
                self.post("error", "Failed to send message.")
                self.conn = None
                
        elif self.offline_ready() and self.offline_mode:
            try:
                send_index = self.drop_send_index
                dd_key = self.derive_deaddrop_key("send", send_index)

                frame = self.frame_message('U', msg.encode())
                blob_key = self.get_offline_blob_key()
                blob = self.e2e.encrypt_offline_blob(frame, blob_key)

                status = await self.deaddrop.put(dd_key, blob)
                #self.post("system", f"[DEBUG PUT STATUS] {status}")

                if status == "OK":
                    self.drop_send_index += 1
                    self.save_offline_state()

                    self.post("me_offline", msg)
                    #self.post("system", f"[OFFLINE] queued via deaddrop key_index={send_index}")
                    self.post("system", f"[OFFLINE] queued and replicated via deaddrops key_index={send_index}")

                elif status == "EXISTS":
                    self.post("error", f"[OFFLINE] key collision at index={send_index}, message not queued")

                else:
                    self.post("error", "[OFFLINE send failed] deaddrop PUT did not succeed")

            except Exception as e:
                self.post("error", f"[OFFLINE send failed] {e}")
                
                
        else:
            if self.is_persistent_mode() and not self.stored_peer:
                self.post("error", "Offline messaging requires a locked peer in persistent mode.")
            else:
                self.post("error", "No active connection. Use /connect <address>")
                
        
            
            
            

    async def connect_to_peer(self, target_address):
        try:
            
            self.current_peer_addr = target_address
            
            reader, writer = await i2plib.stream_connect(
                self.session_id, target_address, sam_address=self.sam_address
            )
            
            
            if hasattr(self, 'my_dest'):
                # Send raw B64 address in single line
                writer.write(self.my_dest.base64.encode() + b"\n")
                # Send 'S' frame to sync state machine
                writer.write(self.frame_message('S', self.my_dest.base64))
                await writer.drain()
                
                # Send E2E key
                writer.write(self.frame_message('K', self.e2e.public_bytes()))
                await writer.drain()
                        
                
                self.proven = True 
                self.network_status = "visible" 
                self.watch_peer_b32(self.peer_b32) 


            self.conn = (reader, writer)
            self.post("success", "Handshake sent. Establishing tunnel...")
            self.run_worker(self.receive_loop(self.conn)) 
            
        
        except Exception as e:
            self.post("error", f"Connection failed: {e}")
            self.conn = None 
            self.post("system", "Waiting for incoming connections...")
        





    async def accept_loop(self):
        while True:
            
            if self.conn:
                await asyncio.sleep(1)
                continue
            
            try:
                reader, writer = await i2plib.stream_accept(self.session_id, sam_address=self.sam_address)
                
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
                    peer_addr = i2plib.Destination(raw_dest).base32 + ".b32.i2p"
                    
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

                    self.post("success", f"Connection accepted from {peer_addr[:12]}...")
                except:
                    peer_addr = "Unknown"

                # Handshake, send OUR identity back in return
                if hasattr(self, 'my_dest'):
                    writer.write(self.frame_message('S', self.my_dest.base64))
                    await writer.drain()
                    
                    # Send E2E key
                    writer.write(self.frame_message('K', self.e2e.public_bytes()))
                    await writer.drain()


                if self.offline_mode:
                    self.leave_offline_mode()
                    self.watch_peer_b32(self.peer_b32)
                    self.post("system", "Leaving OFFLINE mode due to live incoming connection.")

                self.conn = (reader, writer)

                
                # Start receiver
                self.run_worker(self.receive_loop(self.conn))
            
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
                    
                    # Decrypt payload if encrypted
                    if msg_type not in ('K','P','O','S','D'):
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
                self.watch_peer_b32(self.peer_b32)
                
                self.conn = None
                self.current_peer_dest_b64 = None
                self.post("disconnect", "Peer disconnected.")
                self.peer_b32 = "Waiting for incoming connections..."
                self.clear_tofu_runtime_status()
                self.post("system", "Waiting for incoming connections...")

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
                msg = self.pending_messages.pop(delivered_id)

                self.chat_log.write(
                    Align("[dim green] ✓ [/]", align="left"),
                    expand=True
                )

        elif msg_type == 'I':

            if body == "__END__":

                now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
                img_text = "\n".join(self.image_buffer)

                message_panel = Panel(
                    img_text,
                    title=f"[#5f5f5f][{now_utc} UTC][/] [bold cyan]Peer[/]",
                    title_align="left",
                    border_style="cyan",
                    box=box.ROUNDED,
                    expand=False
                )

                self.chat_log.write(Align(message_panel, align="right"), expand=True)
                self.image_buffer = []

            else:
                if len(self.image_buffer) < MAX_IMAGE_LINES:
                    self.image_buffer.append(body)

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

                if "QUIT" in body:
                    self.post("system", "Peer requested disconnect.")

            else:
                try:
                    dest_obj = i2plib.Destination(body)
                    peer_addr = dest_obj.base32 + ".b32.i2p"

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
                    
                    if self.stored_peer_dest_b64:
                        self.set_tofu_verified()
                    else:
                        self.clear_tofu_runtime_status()

                    fp = self.peer_dest_fingerprint(body)
                    self.post("info", f"Peer Identity: {peer_addr} [dim](TOFU {fp})[/]")

                except:
                    pass

        elif msg_type == 'K':
            try:
                self.e2e.receive_peer_key(payload)
                self.post("system", "Secure session established 🔐")
                
                if self.offline_ready():
                    asyncio.create_task(self.send_offline_secret_if_needed())
                
            except Exception as e:
                self.post("error", f"E2E key error: {e}")
                
                
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
                

        elif msg_type == 'P':
            if writer is not None:
                writer.write(self.frame_message('O', b''))
                await writer.drain()





    async def tunnel_watcher(self):
        
        while True:
            if not hasattr(self, 'my_dest'):
                await asyncio.sleep(2)
                continue

            try:
            
                await asyncio.wait_for(
                    i2plib.naming_lookup(
                        self.my_dest.base32 + ".b32.i2p", 
                        sam_address=self.sam_address
                    ), 
                    timeout=5.0
                )
            
            
                if self.network_status != "visible":
                    self.network_status = "visible"
                    self.post("success", "Tunnels confirmed. You are now [bold green]VISIBLE[/].")
                
            except asyncio.TimeoutError:
            
                pass
            except Exception as e:
            
                if self.network_status == "visible":
                    self.network_status = "local_ok"
                    
            await asyncio.sleep(20)




    async def send_file(self, path):
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

        if not self.conn:
            self.post("error", "No active connection")
            return

        reader, writer = self.conn

       
        # Choose renderer
        if mode == "bw":
            lines = render_bw(path)
        else:
            lines = render_braille(path)
            
        if len(lines) > MAX_IMAGE_LINES:
            self.post("error", "Image too large to render safely")
            return
        
        # Show locally with same bubble style
        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
        img_text = "\n".join(lines)

        message_panel = Panel(
            img_text,
            title=f"[#5f5f5f][{now_utc} UTC][/] [bold green]Me[/]",
            title_align="left",
            border_style="green",
            box=box.ROUNDED,
            expand=False
        )

        self.chat_log.write(Align(message_panel, align="left"), expand=True)

        for line in lines:
            
            cipher = self.e2e.encrypt(line.encode())
            writer.write(self.frame_message('I', cipher))
            
        cipher = self.e2e.encrypt(b"__END__")
        writer.write(self.frame_message('I', cipher))

        await writer.drain()

        self.post("success", f"Image sent: {path}")


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
            self.watch_peer_b32(self.peer_b32)
            
            self.conn = None
            self.current_peer_dest_b64 = None
            self.peer_b32 = "Waiting for incoming connections..."
            self.clear_tofu_runtime_status()
            
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
        
        try:
            self.save_offline_state()
        except:
            pass
        
        if self.conn:
            try:
                
                await self.send_control("QUIT")
                _, writer = self.conn
                writer.close()
                
                await writer.wait_closed()
                
            except:
                pass
            
        try:
            if self.sam_writer:
                self.sam_writer.close()
                await self.sam_writer.wait_closed()
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
                
                
                if not hasattr(self, "my_dest"):
                    await asyncio.sleep(5)
                    continue

                if not self.get_offline_peer_b32():
                    await asyncio.sleep(5)
                    continue

                recv_window = self.get_deaddrop_recv_window()
                blob_key = self.get_offline_blob_key()

                for recv_index, dd_key in recv_window:
                    try:
                        blobs = await self.deaddrop.get(dd_key)

                        if not blobs:
                            continue

                        got_valid_blob = False

                        for blob in blobs:
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

                                self.post("system", f"[DROP] received type={msg_type} msg_id={msg_id} key_index={recv_index}")

                            except Exception as e:
                                self.post("error", f"[DROP parse error] {e}")

                        if got_valid_blob:
                            self.consumed_drop_recv.add(recv_index)
                            self.advance_drop_recv_base()
                            self.save_offline_state()

                    except Exception as e:
                        self.post("error", f"[DROP key poll error] {e}")

            except Exception as e:
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



    def show_help(self):

        self.post("help", "Command line options:")
        self.post("help", "  Start with --reset <profile> to recreate a persistent profile from scratch")
        
        self.post("help", "Available commands:")

        self.post("help", "Connection:")
        self.post("help", "  /connect <b32-address>   Connect to peer")
        self.post("help", "  /disconnect              Close connection")

        self.post("help", "Messaging:")
        self.post("help", "  Type text and press ENTER to send message")
        self.post("help", "  /offline                 Enter offline messaging mode (persistent locked peer only)")
        
        self.post("help", "Identity:")
        self.post("help", "  /lock                    Lock persistent profile to current peer (not available in TRANSIENT mode)")

        self.post("help", "Files:")
        self.post("help", "  /sendfile <path>         Send file")

        self.post("help", "Images:")
        self.post("help", "  /img <path>              Send image (braille renderer)")
        self.post("help", "  /img-bw <path>           Send image (block renderer for QR / diagrams)")

        self.post("help", "Utility:")
        self.post("help", "  /help                    Show this help")
        self.post("help", "  /CTRL+q                  Exit program")
        
        



if __name__ == "__main__":
    app = I2PChat()
    app.run()

