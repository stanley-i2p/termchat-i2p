import asyncio


class DeadDropClient:
    def __init__(self, session_id, drops, sam_host="127.0.0.1", sam_port=7656):
        self.session_id = session_id
        self.drops = drops
        self.sam_host = sam_host
        self.sam_port = sam_port

        self.ctrl_reader = None
        self.ctrl_writer = None
        
        self.connect_timeout = 8.0
        self.io_timeout = 8.0

    
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
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.sam_host, self.sam_port),
            timeout=self.connect_timeout
        )

        
        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
        
        # connect
        cmd = f"STREAM CONNECT ID={self.session_id} DESTINATION={destination}\n"

        print("[DD CONNECT]", cmd.strip())

        writer.write(cmd.encode())
        await writer.drain()

        resp = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
        resp_str = resp.decode().strip()

        print("[DD CONNECT RESP]", resp_str)

        if "RESULT=OK" not in resp_str:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"SAM CONNECT FAILED: {resp_str}")

        return reader, writer



    async def _put_one(self, drop: str, key: str, blob: bytes):
        try:
            print("[DD] CONNECTING TO:", drop)
            print("[DD] SESSION:", self.session_id)
            print("[DD] KEY:", key)

            reader, writer = await self._connect(drop)

            writer.write(f"PUT {key} {len(blob)}\n".encode())
            writer.write(blob)
            await asyncio.wait_for(writer.drain(), timeout=self.io_timeout)

            resp = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
            resp_str = resp.decode().strip()
            print("[DD PUT RESP]", resp_str)

            writer.close()
            await writer.wait_closed()

            if resp_str == "OK":
                return (drop, "OK")
            elif resp_str == "EXISTS":
                print(f"[DD PUT] key already exists on {drop}: {key}")
                return (drop, "EXISTS")
            else:
                print(f"[DD PUT] unexpected response from {drop} for key {key}: {resp_str}")
                return (drop, "FAIL")

        except Exception as e:
            print(f"[DROP PUT FAIL] {drop}: {e}")
            return (drop, "FAIL")


    
    # async def put(self, key: str, blob: bytes):
    #     print("[DD] PUT CALLED")
    # 
    #     tasks = [
    #         asyncio.create_task(self._put_one(drop, key, blob))
    #         for drop in self.drops
    #     ]
    # 
    #     results = await asyncio.gather(*tasks, return_exceptions=False)
    # 
    #     ok_count = sum(1 for r in results if r == "OK")
    #     exists_count = sum(1 for r in results if r == "EXISTS")
    # 
    #     if ok_count > 0:
    #         return "OK"
    # 
    #     if exists_count > 0:
    #         return "EXISTS"
    # 
    #     return "FAIL"
    
    
    async def put(self, key: str, blob: bytes):
        print("[DD] PUT CALLED")

        tasks = [
            asyncio.create_task(self._put_one(drop, key, blob))
            for drop in self.drops
        ]

        results = await asyncio.gather(*tasks, return_exceptions=False)

        ok_drops = [drop for drop, status in results if status == "OK"]
        exists_drops = [drop for drop, status in results if status == "EXISTS"]

        if ok_drops:
            return ("OK", ok_drops)

        if exists_drops:
            return ("EXISTS", exists_drops)

        return ("FAIL", [])

 
 
    async def _get_one(self, drop: str, key: str):
        try:
            print("[DD GET] CONNECTING TO:", drop)

            reader, writer = await self._connect(drop)

            writer.write(f"GET {key}\n".encode())
            await asyncio.wait_for(writer.drain(), timeout=self.io_timeout)

            header = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
            print("[DD GET HEADER]", header.decode().strip())

            data = None

            if header.startswith(b"OK"):
                size = int(header.split()[1])
                data = await asyncio.wait_for(reader.readexactly(size), timeout=self.io_timeout)

            writer.close()
            await writer.wait_closed()

            return (drop, data)

        except Exception as e:
            print(f"[DROP GET FAIL] {drop}: {e}")
            return (drop, None)
 
 
 
    # async def get(self, key: str):
    #     tasks = [
    #         asyncio.create_task(self._get_one(drop, key))
    #         for drop in self.drops
    #     ]
    # 
    #     results = await asyncio.gather(*tasks, return_exceptions=False)
    # 
    #     return [data for data in results if data is not None]
    
    
    async def get(self, key: str):
        tasks = [
            asyncio.create_task(self._get_one(drop, key))
            for drop in self.drops
        ]

        results = await asyncio.gather(*tasks, return_exceptions=False)

        good = [(drop, data) for drop, data in results if data is not None]
        return good

    
    async def close(self):
        try:
            if self.ctrl_writer:
                self.ctrl_writer.close()
                await self.ctrl_writer.wait_closed()
        except:
            pass
