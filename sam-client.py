import asyncio


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
        print("[SAM HELLO]", resp.decode().strip())

    
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

        print("[SAM SESSION]", resp_str)

        if "RESULT=OK" not in resp_str:
            raise RuntimeError(f"SAM session failed: {resp_str}")

        return resp_str

    
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

        print("[SAM CONNECT]", cmd.strip())

        writer.write(cmd.encode())
        await writer.drain()

        resp = await reader.readline()
        resp_str = resp.decode().strip()

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
