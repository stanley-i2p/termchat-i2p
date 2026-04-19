import asyncio
import hashlib
import time



# Need to Unify SAM functionality with sam_client.py
# Not critical at all

class DeadDropClient:
    def __init__(self, session_id, drops, sam_host="127.0.0.1", sam_port=7656):
        self.session_id = session_id
        self.put_session_id = f"{session_id}_put"
        self.get_session_id = f"{session_id}_get"
        
        self.drops = drops
        self.sam_host = sam_host
        self.sam_port = sam_port
        
        self.put_ctrl_reader = None
        self.put_ctrl_writer = None

        self.ctrl_reader = None
        self.ctrl_writer = None
        
        self.connect_timeout = 8.0
        self.io_timeout = 8.0
        
        # PoW settings (PUT only)!
        self.pow_prefix = b"POWv1"
        self.pow_zero_bits = 20
        
        # Callback for server profiling
        self.stats_callback = None

    
    # Init Session
    
    async def start(self):
        print("[DD] Starting PUT SAM session:", self.put_session_id)

        self.put_ctrl_reader, self.put_ctrl_writer = await asyncio.open_connection(self.sam_host, self.sam_port)

        self.put_ctrl_writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await self.put_ctrl_writer.drain()
        resp = await self.put_ctrl_reader.readline()
        print("[DD PUT SAM HELLO]", resp.decode().strip())

        cmd = (
            f"SESSION CREATE STYLE=STREAM "
            f"ID={self.put_session_id} "
            f"DESTINATION=TRANSIENT "
            f"SIGNATURE_TYPE=7 "
            f"OPTION inbound.length=2 outbound.length=2 "
            f"inbound.quantity=2 outbound.quantity=2\n"
        )

        self.put_ctrl_writer.write(cmd.encode())
        await self.put_ctrl_writer.drain()

        resp = await self.put_ctrl_reader.readline()
        print("[DD PUT SESSION]", resp.decode().strip())

        print("[DD] Starting GET SAM session:", self.get_session_id)

        self.get_ctrl_reader, self.get_ctrl_writer = await asyncio.open_connection(
            self.sam_host, self.sam_port
        )

        self.get_ctrl_writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await self.get_ctrl_writer.drain()
        resp = await self.get_ctrl_reader.readline()
        print("[DD GET SAM HELLO]", resp.decode().strip())

        cmd = (
            f"SESSION CREATE STYLE=STREAM "
            f"ID={self.get_session_id} "
            f"DESTINATION=TRANSIENT "
            f"SIGNATURE_TYPE=7 "
            f"OPTION inbound.length=2 outbound.length=2 "
            f"inbound.quantity=2 outbound.quantity=2\n"
        )

        self.get_ctrl_writer.write(cmd.encode())
        await self.get_ctrl_writer.drain()

        resp = await self.get_ctrl_reader.readline()
        print("[DD GET SESSION]", resp.decode().strip())

    
    # Stream connect
    
    async def _connect(self, destination, mode="put"):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.sam_host, self.sam_port),
            timeout=self.connect_timeout
        )

        
        writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
        
        # connect
        session_id = self.put_session_id if mode == "put" else self.get_session_id
        cmd = f"STREAM CONNECT ID={session_id} DESTINATION={destination}\n"

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




    def _pow_material(self, key: str, size: int, blob: bytes, pow_counter: int) -> bytes:
        return b"|".join([
            self.pow_prefix,
            key.encode(),
            str(size).encode(),
            blob,
            str(pow_counter).encode(),
        ])

    def _pow_ok(self, digest: bytes) -> bool:
        zero_bytes = self.pow_zero_bits // 8
        rem_bits = self.pow_zero_bits % 8

        if digest[:zero_bytes] != b"\x00" * zero_bytes:
            return False

        if rem_bits == 0:
            return True

        next_byte = digest[zero_bytes]
        mask = 0xFF << (8 - rem_bits)
        return (next_byte & mask) == 0

    def _find_pow_counter(self, key: str, blob: bytes) -> int:
        size = len(blob)
        pow_counter = 0

        while True:
            material = self._pow_material(key, size, blob, pow_counter)
            digest = hashlib.sha256(material).digest()

            if self._pow_ok(digest):
                return pow_counter

            pow_counter += 1




    def _report_stat(self, op: str, drop: str, ok: bool, latency_ms: float, detail: str):
        if self.stats_callback is None:
            return

        try:
            self.stats_callback(op, drop, ok, latency_ms, detail)
        except Exception:
            pass




    async def _put_one(self, drop: str, key: str, blob: bytes, pow_counter: int):
        started = time.monotonic()

        try:
            print("[DD] CONNECTING TO:", drop)
            print("[DD] SESSION:", self.put_session_id)
            print("[DD] KEY:", key)

            size = len(blob)

            reader, writer = await self._connect(drop, mode="put")

            writer.write(f"PUT {key} {size} {pow_counter}\n".encode())
            writer.write(blob)
            await asyncio.wait_for(writer.drain(), timeout=self.io_timeout)

            resp = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
            resp_str = resp.decode().strip()
            print("[DD PUT RESP]", resp_str)

            writer.close()
            await writer.wait_closed()

            latency_ms = (time.monotonic() - started) * 1000.0

            if resp_str == "OK":
                self._report_stat("put", drop, True, latency_ms, "OK")
                return (drop, "OK")
            elif resp_str == "EXISTS":
                print(f"[DD PUT] key already exists on {drop}: {key}")
                self._report_stat("put", drop, True, latency_ms, "EXISTS")
                return (drop, "EXISTS")
            else:
                print(f"[DD PUT] unexpected response from {drop} for key {key}: {resp_str}")
                self._report_stat("put", drop, False, latency_ms, resp_str or "FAIL")
                return (drop, "FAIL")

        except Exception as e:
            latency_ms = (time.monotonic() - started) * 1000.0
            print(f"[DROP PUT FAIL] {drop}: {e}")
            self._report_stat("put", drop, False, latency_ms, type(e).__name__)
            return (drop, "FAIL")


    
    
    
    async def put(self, key: str, blob: bytes):
        print("[DD] PUT CALLED")

        pow_counter = self._find_pow_counter(key, blob)

        tasks = [
            asyncio.create_task(self._put_one(drop, key, blob, pow_counter))
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
        started = time.monotonic()

        try:
            print("[DD GET] CONNECTING TO:", drop)
            
            print("[DD] SESSION:", self.get_session_id)
            reader, writer = await self._connect(drop, mode="get")

            writer.write(f"GET {key}\n".encode())
            await asyncio.wait_for(writer.drain(), timeout=self.io_timeout)

            header = await asyncio.wait_for(reader.readline(), timeout=self.io_timeout)
            resp_str = header.decode().strip()
            print("[DD GET HEADER]", resp_str)

            data = None

            if header.startswith(b"OK"):
                size = int(header.split()[1])
                data = await asyncio.wait_for(reader.readexactly(size), timeout=self.io_timeout)

            writer.close()
            await writer.wait_closed()

            latency_ms = (time.monotonic() - started) * 1000.0

            if header.startswith(b"OK"):
                self._report_stat("get", drop, True, latency_ms, "OK")
            elif header.startswith(b"MISS"):
                self._report_stat("get", drop, True, latency_ms, "MISS")
            else:
                self._report_stat("get", drop, False, latency_ms, resp_str or "FAIL")

            return (drop, data)

        except Exception as e:
            latency_ms = (time.monotonic() - started) * 1000.0
            print(f"[DROP GET FAIL] {drop}: {e}")
            self._report_stat("get", drop, False, latency_ms, type(e).__name__)
            return (drop, None)
 
 
 
    
    
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
            if self.put_ctrl_writer:
                self.put_ctrl_writer.close()
                await self.put_ctrl_writer.wait_closed()
        except:
            pass

        try:
            if self.get_ctrl_writer:
                self.get_ctrl_writer.close()
                await self.get_ctrl_writer.wait_closed()
        except:
            pass
