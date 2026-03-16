# Python 3.10+ compatibility
# Use ./libi2p
import sys, os
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
PROFILE_NAME = os.path.basename(sys.argv[1]) if len(sys.argv) > 1 else "default"

PROFILE_DIR = os.path.join(BASE_DIR, "profiles", PROFILE_NAME)

IMAGE_DIR = os.path.join(BASE_DIR, "images")
FILE_DIR = os.path.join(BASE_DIR, "files")
BLOB_DIR = os.path.join(BASE_DIR, "blobs")

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
        self.current_peer_addr = None
        self.profile = sys.argv[1] if len(sys.argv) > 1 else "default"
    
        # Generate a unique ID for THIS appinstance
        self.session_id = f"chat_{self.profile}_{int(time.time())}"
        self.proven = False  
        
        # File transfer states
        self.incoming_file = None
        self.incoming_filename = None
        self.incoming_expected = 0
        self.incoming_received = 0
        
        self.image_buffer = []
        
        self.pending_messages = {}
        
        self.e2e = E2E()


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

        
        left_content = f"[black on {tag_bg}] [bold]{mode_tag}[/] [/] [bold]{self.profile.upper()}[/]"

        

        if is_active:
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
        
        
        
        if type_name in ["me", "peer"]:
            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
            
            
            box_color = "green" if type_name == "me" else "cyan"
            display_name = "Me" if type_name == "me" else "Peer"
            alignment = "left" if type_name == "me" else "right"
            
            
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
        
        if msg_type not in b"UDISFCEKPO":
            raise ValueError("Unknown frame type")

    
        if length < 0 or length > MAX_FRAME_SIZE:
            raise ValueError("Invalid frame size")

        payload = await reader.readexactly(length)

        return msg_type.decode(), msg_id, payload
    
    
    
    def generate_msg_id(self):
        return (int(time.time() * 1000) ^ random.getrandbits(32)) & 0xFFFFFFFFFFFFFFFF



    async def on_mount(self):
        
        self.chat_log = self.query_one("#chat_window", RichLog)
        
        self.network_status = "initializing"
        
        self.peer_b32 = "Initializing SAM Session..."
        
        
        
        self.post("system", "Initializing SAM Session...")
        self.post("system", f"Initializing Profile: [bold yellow]{self.profile}[/]")
        
        
        
        is_persistent = len(sys.argv) > 1
        
        
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
                    self.post("system", f"Loaded identity from [cyan]{key_file}[/]")
                
                
                if len(lines) > 1:
                    self.stored_peer = lines[1]
                    self.post("system", f"Stored Contact: [cyan]{self.stored_peer}.b32.i2p[/]")
                    
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
                self.post("system", "Type [bold yellow]/connect[/] to dial stored contact.")
                self.post("system", "Waiting for incoming connections...")
            else:
                self.post("system", "Waiting for incoming connections...")

            self.run_worker(self.accept_loop())
            
            
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

        if msg.startswith("/connect"):
            if self.conn:
                self.post("error", "Already connected. Use /disconnect first.")
                return
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
                
                
        elif msg.strip() == "/save":
            if len(sys.argv) <= 1:
                self.post("error", "Cannot save in [bold green]TRANSIENT[/] mode. Restart with a profile name.")
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
                    with open(key_file, "a") as f:
                        f.write(self.current_peer_addr + "\n")
                    self.stored_peer = self.current_peer_addr
                    self.post("success", f"Identity [bold yellow]{self.profile}[/] is now locked to this peer.")
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
        else:
            self.post("error", "No active connection. Use /connect <address>")
            
            
            

    async def connect_to_peer(self, target_address):
        try:
            
            self.current_peer_addr = target_address
            
            reader, writer = await i2plib.stream_connect(
                self.session_id, target_address, sam_address=self.sam_address
            )
            
            
            if hasattr(self, 'my_dest'):
                # Send the raw B64 address in single line
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
                    
                    # If profile is LOCKED, verify the calling peer
                    if self.stored_peer and peer_addr != self.stored_peer:
                        self.post("error", f"Blocked unauthorized call from {peer_addr}...")
                        writer.close()
                        continue
                 
                    
                    self.current_peer_addr = peer_addr
                    self.peer_b32 = peer_addr 
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

                body = payload.decode('utf-8', errors="ignore")

                # Routing block
                if msg_type == 'U':
                    self.post("peer", body)
                    
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

                        
                        self.chat_log.write(Align("[dim green] ✓ [/]", align="left"),
                        expand=True)

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
                        self.incoming_filename = safe_name
                        self.incoming_expected = size
                        self.incoming_received = 0

                        self.post("system", f"Receiving file: {safe_name} ({size} bytes)")

                    except Exception as e:
                        self.post("error", f"Invalid file header: {e}")

                elif msg_type == 'C':

                    try:
                        if self.incoming_file:

                            chunk = base64.b64decode(payload)

                            self.incoming_received += len(chunk)

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

                elif msg_type == 'S':

                    if "__SIGNAL__:" in body:

                        if "QUIT" in body:
                            self.post("system", "Peer requested disconnect.")
                            break

                    else:

                        try:
                            dest_obj = i2plib.Destination(body)

                            self.peer_b32 = dest_obj.base32 + ".b32.i2p"
                            peer_addr = self.peer_b32

                            self.post("info", f"Peer Identity: {peer_addr}")

                        except:
                            pass
                        
                        
                elif msg_type == 'K':

                    try:
                        self.e2e.receive_peer_key(payload)
                        self.post("system", "Secure session established 🔐")
                    except Exception as e:
                        self.post("error", f"E2E key error: {e}")
                        

                elif msg_type == 'P':

                    writer.write(self.frame_message('O', b''))
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass

        except Exception as e:
            if self.conn == connection:
                self.post("error", f"Protocol Error: {e}")

        finally:
            if self.conn == connection:
                self.conn = None
                self.post("disconnect", "Peer disconnected.")
                self.peer_b32 = "Waiting for incoming connections..."
                self.post("system", "Waiting for incoming connections...")

            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass





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
            
            if filesize > MAX_FILE_SIZE:
                self.post("error", f"File too large ({filesize} bytes)")
                return

            self.post("system", f"Sending file: {filename} ({filesize} bytes)")

            
            header = f"{filename}|{filesize}"
            
            cipher = self.e2e.encrypt(header.encode())
            writer.write(self.frame_message('F', cipher))
            
            await writer.drain()

            with open(path, "rb") as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break

                    encoded = base64.b64encode(chunk).decode()
                    
                    cipher = self.e2e.encrypt(encoded.encode())
                    writer.write(self.frame_message('C', cipher))
                    
                    await writer.drain()

            # End transfer
            writer.write(self.frame_message('E', ''))
            await writer.drain()

            self.post("success", f"File sent: {filename}")

        except Exception as e:
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
            self.conn = None 
            self.peer_b32 = "Waiting for incoming connections..." 
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



    def safe_filename(name):
        return os.path.basename(name)



    def show_help(self):

        self.post("help", "Available commands:")

        self.post("help", "Connection:")
        self.post("help", "  /connect <b32-address>   Connect to peer")
        self.post("help", "  /disconnect              Close connection")

        self.post("help", "Messaging:")
        self.post("help", "  Type text and press ENTER to send message")
        
        self.post("help", "Identity:")
        self.post("help", "  /save                    Save identity (not available in TRANSIENT mode)")

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

