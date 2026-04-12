import asyncio
import base64
import hashlib

DEBUG = False


class SAMClient:
    def __init__(self, sam_host="127.0.0.1", sam_port=7656):
        self.sam_host = sam_host
        self.sam_port = sam_port
        self.session_id = None
        self.ctrl_reader = None
        self.ctrl_writer = None

    
    # CONNECT + HELLO
    
    async def connect(self):
        self.ctrl_reader, self.ctrl_writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        self.ctrl_writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await self.ctrl_writer.drain()

        resp = await self.ctrl_reader.readline()
        if DEBUG:
            print("[SAM HELLO]", resp.decode().strip())
        
        
    
    def destination_to_b32(self, dest_b64: str) -> str:
        # Replace in destination base64. Uses (- ~) instead of (+ /)
        std_b64 = dest_b64.replace("-", "+").replace("~", "/")
        padding = "=" * (-len(std_b64) % 4)
        raw = base64.b64decode(std_b64 + padding)

        h = hashlib.sha256(raw).digest()
        b32 = base64.b32encode(h).decode().lower().rstrip("=")
        return b32 + ".b32.i2p"


    async def generate_destination(self, sig_type=7):
        cmd = f"DEST GENERATE SIGNATURE_TYPE={sig_type}\n"
        self.ctrl_writer.write(cmd.encode())
        await self.ctrl_writer.drain()

        resp = await self.ctrl_reader.readline()
        resp_str = resp.decode().strip()
        if DEBUG:
            print("[SAM DEST GENERATE]", resp_str)

        parts = resp_str.split()

        pub = None
        priv = None

        for part in parts:
            if part.startswith("PUB="):
                pub = part.split("=", 1)[1]
            elif part.startswith("PRIV="):
                priv = part.split("=", 1)[1]

        if not pub or not priv:
            raise RuntimeError(f"DEST GENERATE failed: {resp_str}")

        return pub, priv


    async def naming_lookup(self, name: str) -> str:
        cmd = f"NAMING LOOKUP NAME={name}\n"
        self.ctrl_writer.write(cmd.encode())
        await self.ctrl_writer.drain()

        resp = await self.ctrl_reader.readline()
        resp_str = resp.decode().strip()
        if DEBUG:
            print("[SAM LOOKUP]", resp_str)

        parts = resp_str.split()

        result = None
        value = None

        for part in parts:
            if part.startswith("RESULT="):
                result = part.split("=", 1)[1]
            elif part.startswith("VALUE="):
                value = part.split("=", 1)[1]

        if result != "OK" or not value:
            raise RuntimeError(f"NAMING LOOKUP failed: {resp_str}")

        return value
    
    

    
    # CREATE SESSION
    
    async def create_session(self, session_id, destination="TRANSIENT", options=None):
        self.session_id = session_id

        if options is None:
            options = {
                "inbound.length": 2,
                "outbound.length": 2,
                "inbound.quantity": 2,
                "outbound.quantity": 2,
            }

        options_str = " ".join(f"{k}={v}" for k, v in options.items())

        cmd = (
            f"SESSION CREATE STYLE=STREAM "
            f"ID={session_id} "
            f"DESTINATION={destination} "
            f"SIGNATURE_TYPE=7 "
            f"OPTION {options_str}\n"
        )

        self.ctrl_writer.write(cmd.encode())
        await self.ctrl_writer.drain()

        resp = await self.ctrl_reader.readline()
        resp_str = resp.decode().strip()

        if DEBUG:
            print("[SAM SESSION]", resp_str)

        if "RESULT=OK" not in resp_str:
            raise RuntimeError(f"SAM session failed: {resp_str}")
        
        destination = None
        for part in resp_str.split():
            if part.startswith("DESTINATION="):
                destination = part.split("=", 1)[1]
                break

        return destination if destination else resp_str


    
    # STREAM CONNECT
    
    async def stream_connect(self, destination_b32):
        reader, writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        # HELLO
        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await reader.readline()

        cmd = f"STREAM CONNECT ID={self.session_id} DESTINATION={destination_b32}\n"

        if DEBUG:
            print("[SAM CONNECT]", cmd.strip())
        

        writer.write(cmd.encode())
        await writer.drain()

        resp = await reader.readline()
        resp_str = resp.decode().strip()

        if DEBUG:
            print("[SAM CONNECT RESP]", resp_str)
            


        if "RESULT=OK" not in resp_str:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"CONNECT failed: {resp_str}")

        return reader, writer

    
    # STREAM ACCEPT (server)
    
    async def stream_accept(self):
        reader, writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await reader.readline()

        cmd = f"STREAM ACCEPT ID={self.session_id}\n"

        writer.write(cmd.encode())
        await writer.drain()

        resp = await reader.readline()

        if b"RESULT=OK" not in resp:
            raise RuntimeError(f"ACCEPT failed: {resp.decode()}")

        return reader, writer

    
    # CLOSE
    
    async def close(self):
        try:
            if self.ctrl_writer:
                self.ctrl_writer.close()
                await self.ctrl_writer.wait_closed()
        except:
            pass
