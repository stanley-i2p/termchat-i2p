import asyncio


class DeadDropClient:
    def __init__(self, session_id, drops, sam_host="127.0.0.1", sam_port=7656):
        self.session_id = session_id
        self.drops = drops
        self.sam_host = sam_host
        self.sam_port = sam_port

        self.ctrl_reader = None
        self.ctrl_writer = None

    
    # Init Session
    
    async def start(self):
        print("[DD] Starting SAM session:", self.session_id)

        self.ctrl_reader, self.ctrl_writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        
        self.ctrl_writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await self.ctrl_writer.drain()
        resp = await self.ctrl_reader.readline()
        print("[DD SAM HELLO]", resp.decode().strip())

        
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
        print("[DD SESSION]", resp.decode().strip())

    
    # Stream connect
    
    async def _connect(self, destination):
        reader, writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        
        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await reader.readline()

        # connect
        cmd = f"STREAM CONNECT ID={self.session_id} DESTINATION={destination}\n"

        print("[DD CONNECT]", cmd.strip())

        writer.write(cmd.encode())
        await writer.drain()

        resp = await reader.readline()
        resp_str = resp.decode().strip()

        print("[DD CONNECT RESP]", resp_str)

        if "RESULT=OK" not in resp_str:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"SAM CONNECT FAILED: {resp_str}")

        return reader, writer

    
    async def put(self, key: str, blob: bytes):
        print("[DD] PUT CALLED")

        for drop in self.drops:
            try:
                print("[DD] CONNECTING TO:", drop)
                print("[DD] SESSION:", self.session_id)
                print("[DD] KEY:", key)

                reader, writer = await self._connect(drop)

                writer.write(f"PUT {key} {len(blob)}\n".encode())
                writer.write(blob)
                await writer.drain()

                resp = await reader.readline()
                resp_str = resp.decode().strip()
                print("[DD PUT RESP]", resp_str)
                
                writer.close()
                await writer.wait_closed()

                if resp_str == "OK":
                    return "OK"
                elif resp_str == "EXISTS":
                    print(f"[DD PUT] key already exists: {key}")
                    return "EXISTS"
                else:
                    print(f"[DD PUT] unexpected response for key {key}: {resp_str}")
                    return "FAIL"


            except Exception as e:
                print(f"[DROP PUT FAIL] {drop}: {e}")

    
    async def get(self, key: str):
        results = []

        for drop in self.drops:
            try:
                print("[DD GET] CONNECTING TO:", drop)

                reader, writer = await self._connect(drop)

                writer.write(f"GET {key}\n".encode())
                await writer.drain()

                header = await reader.readline()
                print("[DD GET HEADER]", header.decode().strip())

                if header.startswith(b"OK"):
                    size = int(header.split()[1])
                    data = await reader.readexactly(size)
                    results.append(data)

                writer.close()
                await writer.wait_closed()

            except Exception as e:
                print(f"[DROP GET FAIL] {drop}: {e}")

        return results

    
    async def close(self):
        try:
            if self.ctrl_writer:
                self.ctrl_writer.close()
                await self.ctrl_writer.wait_closed()
        except:
            pass
