import asyncio
import os
import hashlib
import signal
import time

shutdown_event = asyncio.Event()

drop_semaphores = {}
drop_put_times = {}
drop_rate_lock = asyncio.Lock()



BASE_DIR = os.path.expanduser("~/.termchat-server")
IDENTITY_DIR = os.path.join(BASE_DIR, "identities")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")


# PLEASE: for Production try to use 1.
# Having multiple instances of deaddrop-server on 1 physical server will
# NOT increase fault tolerance of overall offline ecosystem.
#
# Numbers greater than 1 are useful when debugging the deaddrop replication feature.!!!
#
# Since adding the anti flood PoW system, debugging with more than 1 instance is no
# longer advisable
NUM_DROPS = 1

SAM_HOST = "127.0.0.1"
SAM_PORT = 7656

SAM_CONFIG = {
    "inbound.length": 2,
    "outbound.length": 2,
    "inbound.quantity": 2,
    "outbound.quantity": 2,
}


# Blob retention
BLOB_TTL_SECONDS = 14 * 24 * 60 * 60   # 14 days ?
GC_INTERVAL_SECONDS = 60 * 60          # 1 hour ?

# Basic hardening
MAX_BLOB_SIZE = 256 * 1024            # 256 KB
MAX_KEY_LEN = 128
CLIENT_READ_TIMEOUT = 15.0
CLIENT_WRITE_TIMEOUT = 15.0
MAX_ACTIVE_CLIENTS_PER_DROP = 32

# Simple per-drop PUT rate limit
PUT_RATE_WINDOW_SECONDS = 60
MAX_PUTS_PER_WINDOW_PER_DROP = 500

# PUT PoW
POW_PREFIX = b"POWv1"
POW_ZERO_BITS = 20



def drop_storage_dir(drop_name: str):
    return os.path.join(STORAGE_DIR, drop_name)


def ensure_dirs():
    os.makedirs(IDENTITY_DIR, exist_ok=True)
    os.makedirs(STORAGE_DIR, exist_ok=True)

    for i in range(NUM_DROPS):
        drop_name = f"drop_{i}"
        os.makedirs(drop_storage_dir(drop_name), exist_ok=True)
        drop_semaphores[drop_name] = asyncio.Semaphore(MAX_ACTIVE_CLIENTS_PER_DROP)
        drop_put_times[drop_name] = []
        
        


def blob_path(drop_name: str, key: str):
    h = hashlib.sha256(key.encode()).hexdigest()
    sub = os.path.join(drop_storage_dir(drop_name), h[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, h)



def is_valid_key(key: str) -> bool:
    if not key:
        return False

    if len(key) > MAX_KEY_LEN:
        return False

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    return all(ch in allowed for ch in key)



def is_blob_expired(path: str, now: float) -> bool:
    try:
        mtime = os.path.getmtime(path)
        return (now - mtime) > BLOB_TTL_SECONDS
    except FileNotFoundError:
        return False
    except Exception:
        return False



async def allow_put(drop_name: str) -> bool:
    now = time.time()

    async with drop_rate_lock:
        times = drop_put_times[drop_name]

        cutoff = now - PUT_RATE_WINDOW_SECONDS
        while times and times[0] < cutoff:
            times.pop(0)

        if len(times) >= MAX_PUTS_PER_WINDOW_PER_DROP:
            return False

        times.append(now)
        return True




def pow_material(key: str, size: int, blob: bytes, pow_counter: int) -> bytes:
    return b"|".join([
        POW_PREFIX,
        key.encode(),
        str(size).encode(),
        blob,
        str(pow_counter).encode(),
    ])


def pow_ok(digest: bytes) -> bool:
    zero_bytes = POW_ZERO_BITS // 8
    rem_bits = POW_ZERO_BITS % 8

    if digest[:zero_bytes] != b"\x00" * zero_bytes:
        return False

    if rem_bits == 0:
        return True

    next_byte = digest[zero_bytes]
    mask = 0xFF << (8 - rem_bits)
    return (next_byte & mask) == 0


def verify_put_pow(key: str, size: int, blob: bytes, pow_counter: int) -> bool:
    material = pow_material(key, size, blob, pow_counter)
    digest = hashlib.sha256(material).digest()
    return pow_ok(digest)




async def gc_loop():
    while not shutdown_event.is_set():
        try:
            now = asyncio.get_running_loop().time()
            deleted = 0

            for i in range(NUM_DROPS):
                drop_name = f"drop_{i}"
                drop_root = drop_storage_dir(drop_name)

                for root, _, files in os.walk(drop_root):
                    for name in files:
                        path = os.path.join(root, name)

                        try:
                            st = os.stat(path)
                            age = time.time() - st.st_mtime

                            if age > BLOB_TTL_SECONDS:
                                os.remove(path)
                                deleted += 1
                        except FileNotFoundError:
                            continue
                        except Exception as e:
                            print(f"[GC] failed to remove {path}: {e}")


            if deleted:
                print(f"[GC] removed {deleted} expired blobs")

        except Exception as e:
            print(f"[GC] loop error: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=GC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass



# Storage Protocol


async def handle_client(drop_name, reader, writer):

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=CLIENT_READ_TIMEOUT)
        
        #print(f"[SERVER] raw line: {line}")
        
        if not line:
            return
        
        if len(line) > 512:
            writer.write(b"ERR\n")
            await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
            print(f"[{drop_name}] rejected oversized command line")
            return

        parts = line.decode(errors="ignore").strip().split()
        
        #print(f"[SERVER] parsed: {parts}")

        if not parts:
            return

        cmd = parts[0]

        
        # PUT CMD
        
        if cmd == "PUT" and len(parts) >= 4:

            key = parts[1]

            if not is_valid_key(key):
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT invalid key")
                return

            try:
                size = int(parts[2])
            except ValueError:
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT invalid size")
                return

            try:
                pow_counter = int(parts[3])
            except ValueError:
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT key={key} size={size} result=REJECT_POW_COUNTER")
                return

            if pow_counter < 0:
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT key={key} size={size} result=REJECT_POW_COUNTER")
                return

            if size < 0 or size > MAX_BLOB_SIZE:
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT key={key} size={size} result=REJECT_SIZE")
                return

            if not await allow_put(drop_name):
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT key={key} size={size} result=RATE_LIMIT")
                return

            data = await asyncio.wait_for(reader.readexactly(size), timeout=CLIENT_READ_TIMEOUT)

            if not verify_put_pow(key, size, data, pow_counter):
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] PUT key={key} size={size} result=REJECT_POW")
                return

            path = blob_path(drop_name, key)

            if os.path.exists(path):
                writer.write(b"EXISTS\n")
                print(f"[{drop_name}] PUT key={key} size={size} result=EXISTS")
            else:
                with open(path, "wb") as f:
                    f.write(data)

                writer.write(b"OK\n")
                print(f"[{drop_name}] PUT key={key} size={size} result=OK")

        
        # GET CMD
        
        elif cmd == "GET" and len(parts) >= 2:

            key = parts[1]

            if not is_valid_key(key):
                writer.write(b"ERR\n")
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)
                print(f"[{drop_name}] GET invalid key")
                return

            path = blob_path(drop_name, key)

            if not os.path.exists(path):
                writer.write(b"MISS\n")
                print(f"[{drop_name}] GET key={key} result=MISS")

            else:
                try:
                    age = time.time() - os.path.getmtime(path)

                    if age > BLOB_TTL_SECONDS:
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            pass

                        writer.write(b"MISS\n")
                        print(f"[{drop_name}] GET key={key} result=MISS_EXPIRED")
                    else:
                        with open(path, "rb") as f:
                            data = f.read()

                        writer.write(f"OK {len(data)}\n".encode())
                        writer.write(data)
                        print(f"[{drop_name}] GET key={key} size={len(data)} result=OK")

                except FileNotFoundError:
                    writer.write(b"MISS\n")
                    print(f"[{drop_name}] GET key={key} result=MISS")

        else:
            writer.write(b"ERR\n")

        await asyncio.wait_for(writer.drain(), timeout=CLIENT_WRITE_TIMEOUT)

    except asyncio.CancelledError:
        raise

    except asyncio.TimeoutError:
        print(f"[{drop_name}] client timeout")

    except Exception as e:
        print(f"[ERROR] client handling: {e}")
        

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass



# SAM raw control (need to get rid of libi2p in client also)


async def sam_cmd(cmd: str):

    reader, writer = await asyncio.open_connection(SAM_HOST, SAM_PORT)

    writer.write((cmd + "\n").encode())
    await writer.drain()

    resp = await reader.readline()

    writer.close()
    await writer.wait_closed()

    return resp.decode().strip()




async def create_session(name, keyfile):

    reader, writer = await asyncio.open_connection(SAM_HOST, SAM_PORT)

    # Handshake
    writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
    await writer.drain()

    hello_resp = await reader.readline()

    if not hello_resp:
        raise RuntimeError("SAM did not respond to HELLO")

    print(f"[{name}] HELLO: {hello_resp.decode().strip()}")

    
    # Load or create destination
    
    if os.path.exists(keyfile):
        with open(keyfile, "r") as f:
            dest = f.read().strip()
    else:
        dest = "TRANSIENT"

    
    # Session create
    
    options_str = " ".join(f"{k}={v}" for k, v in SAM_CONFIG.items())

    cmd = (
        f"SESSION CREATE STYLE=STREAM "
        f"ID={name} "
        f"DESTINATION={dest} "
        f"SIGNATURE_TYPE=7 "
        f"OPTION {options_str}\n"
    )

    writer.write(cmd.encode())
    await writer.drain()

    resp = await reader.readline()

    if not resp:
        raise RuntimeError("SAM did not respond to SESSION CREATE")

    resp_str = resp.decode().strip()
    print(f"[{name}] {resp_str}")

    if "RESULT=OK" not in resp_str:
        raise RuntimeError(f"Failed to create session: {resp_str}")

    
    # Destination extraction
    
    if "DESTINATION=" in resp_str:

        parts = resp_str.split()

        for part in parts:
            if part.startswith("DESTINATION="):
                dest_b64 = part.split("=", 1)[1]

                with open(keyfile, "w") as f:
                    f.write(dest_b64)

                break

    return reader, writer



async def accept_loop(name):

    while not shutdown_event.is_set():

        try:
            reader, writer = await asyncio.open_connection(SAM_HOST, SAM_PORT)

            
            writer.write(b"HELLO VERSION MIN=3.0 MAX=3.2\n")
            await writer.drain()
            await reader.readline()

            
            writer.write(f"STREAM ACCEPT ID={name}\n".encode())
            await writer.drain()

            resp = await reader.readline()

            if b"RESULT=OK" not in resp:
                print(f"[{name}] ACCEPT failed: {resp.decode().strip()}")
                await asyncio.sleep(1)
                continue

            #print(f"[{name}] waiting...")

            # Wait for incoming connection
            dest_line = await reader.readline()

            if not dest_line:
                continue

            #print(f"[{name}] incoming from: {dest_line[:60]}")

            sem = drop_semaphores[name]

            if sem.locked():
                print(f"[{name}] connection rejected: too many active clients")
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass
                continue

            async with sem:
                await handle_client(name, reader, writer)

        except asyncio.CancelledError:
            break

        except Exception as e:
            print(f"[{name}] accept error: {e}")
            await asyncio.sleep(1)




async def main():

    ensure_dirs()

    loop = asyncio.get_running_loop()

    def _shutdown():
        print("\n[INFO] Shutdown signal received")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, _shutdown)
    loop.add_signal_handler(signal.SIGTERM, _shutdown)

    tasks = []

    # Keep sessions alive
    session_conns = []

    for i in range(NUM_DROPS):

        name = f"drop_{i}"
        keyfile = os.path.join(IDENTITY_DIR, f"{name}.dat")

        # Create persistent SAM session
        session_reader, session_writer = await create_session(name, keyfile)

        # Store reference so it's not garbage collected / closed
        session_conns.append((session_reader, session_writer))

        # Accept loop gets new connections
        task = asyncio.create_task(
            accept_loop(name)
        )

        tasks.append(task)
        
    gc_task = asyncio.create_task(gc_loop())
    tasks.append(gc_task)

    print(f"[INFO] Started {NUM_DROPS} drop identities")

    
    await shutdown_event.wait()

    print("[INFO] Shutting down accept loops...")

    for t in tasks:
        t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    print("[INFO] Closing SAM sessions...")

    for reader, writer in session_conns:
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass

    print("[INFO] Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
