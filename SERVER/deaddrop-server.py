import asyncio
import os
import hashlib
import signal
import time

shutdown_event = asyncio.Event()



BASE_DIR = os.path.expanduser("~/.termchat-server")
IDENTITY_DIR = os.path.join(BASE_DIR, "identities")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")

# PLEASE: for Production try to use 1.
# Having multiple instances of deaddrop-server on 1 physical server will
# NOT increase fault tolerance of overall offline ecosystem.
# Numbers > 1 are usefull when debugging deaddrop replication feature!!!
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




def ensure_dirs():
    os.makedirs(IDENTITY_DIR, exist_ok=True)
    os.makedirs(STORAGE_DIR, exist_ok=True)


def blob_path(key: str):
    h = hashlib.sha256(key.encode()).hexdigest()
    sub = os.path.join(STORAGE_DIR, h[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, h)


def is_blob_expired(path: str, now: float) -> bool:
    try:
        mtime = os.path.getmtime(path)
        return (now - mtime) > BLOB_TTL_SECONDS
    except FileNotFoundError:
        return False
    except Exception:
        return False


async def gc_loop():
    while not shutdown_event.is_set():
        try:
            now = asyncio.get_running_loop().time()
            deleted = 0

            for root, _, files in os.walk(STORAGE_DIR):
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


async def handle_client(reader, writer):

    try:
        line = await reader.readline()
        
        print(f"[SERVER] raw line: {line}")
        
        if not line:
            return

        parts = line.decode(errors="ignore").strip().split()
        
        print(f"[SERVER] parsed: {parts}")

        if not parts:
            return

        cmd = parts[0]

        
        # PUT CMD
        
        if cmd == "PUT" and len(parts) >= 3:

            key = parts[1]
            size = int(parts[2])

            print(f"[SERVER] PUT key={key} size={size}")

            data = await reader.readexactly(size)

            path = blob_path(key)

            if os.path.exists(path):
                print(f"[SERVER] PUT exists key={key}")
                writer.write(b"EXISTS\n")
            else:
                with open(path, "wb") as f:
                    f.write(data)

                writer.write(b"OK\n")
                print("[SERVER] PUT stored and ACK sent")

        
        # GET CMD
        
        elif cmd == "GET" and len(parts) >= 2:

            key = parts[1]
            path = blob_path(key)

            if not os.path.exists(path):
                print(f"[SERVER] GET miss key={key}")
                writer.write(b"MISS\n")

            else:
                try:
                    age = time.time() - os.path.getmtime(path)

                    if age > BLOB_TTL_SECONDS:
                        try:
                            os.remove(path)
                            print(f"[SERVER] GET expired key={key} removed")
                        except FileNotFoundError:
                            pass

                        writer.write(b"MISS\n")
                    else:
                        with open(path, "rb") as f:
                            data = f.read()

                        print(f"[SERVER] GET hit key={key} size={len(data)}")
                        writer.write(f"OK {len(data)}\n".encode())
                        writer.write(data)

                except FileNotFoundError:
                    writer.write(b"MISS\n")

        else:
            writer.write(b"ERR\n")

        await writer.drain()

    except asyncio.CancelledError:
        raise
    
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

            print(f"[{name}] waiting...")

            # Wait for incoming connection
            dest_line = await reader.readline()

            if not dest_line:
                continue

            print(f"[{name}] incoming from: {dest_line[:60]}")

            
            await handle_client(reader, writer)

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
