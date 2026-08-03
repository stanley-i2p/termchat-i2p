import asyncio

from sam_client import SAMClient


SAM_CONNECT_CANCEL_GRACE = 4.5


class SamRuntimeClosed(Exception):
    pass


class SamSessionManager:
    def __init__(self, sam_host="127.0.0.1", sam_port=7656):
        self.client = SAMClient(sam_host, sam_port)
        self.closing_client = None
        self.closing = False
        self.accept_cancel_event = asyncio.Event()
        self.connect_cancel_event = asyncio.Event()
        self.accept_tasks = set()
        self.connect_tasks = set()
        self.send_tasks = set()
        self.streams = []

    def is_closing(self):
        return self.closing

    async def connect(self):
        if self.closing:
            raise SamRuntimeClosed()
        await self.client.connect()

    async def create_session(self, *args, **kwargs):
        if self.closing:
            raise SamRuntimeClosed()
        return await self.client.create_session(*args, **kwargs)

    async def naming_lookup(self, *args, **kwargs):
        if self.closing:
            raise SamRuntimeClosed()
        return await self.client.naming_lookup(*args, **kwargs)

    async def generate_destination(self, *args, **kwargs):
        if self.closing:
            raise SamRuntimeClosed()
        return await self.client.generate_destination(*args, **kwargs)

    def destination_to_b32(self, dest_b64: str) -> str:
        return self.client.destination_to_b32(dest_b64)

    def begin_closing(self):
        if self.closing:
            return
        self.closing = True
        self.closing_client = self.client
        self.accept_cancel_event.set()
        self.connect_cancel_event.set()
        self.cancel_tasks(self.send_tasks)
        self.cancel_tasks(self.accept_tasks)
        self.cancel_tasks(self.connect_tasks)

    def cancel_tasks(self, tasks):
        for task in list(tasks):
            if not task.done():
                task.cancel()

    def track_accept_task(self, task):
        self.accept_tasks.add(task)
        task.add_done_callback(lambda done: self.accept_tasks.discard(done))
        return task

    def track_connect_task(self, task):
        self.connect_tasks.add(task)
        task.add_done_callback(lambda done: self.connect_tasks.discard(done))
        return task

    def track_send_task(self, task):
        self.send_tasks.add(task)
        task.add_done_callback(lambda done: self.send_tasks.discard(done))
        return task

    def register_stream(self, reader, writer):
        if writer is None:
            return
        for _, existing_writer in self.streams:
            if existing_writer is writer:
                return
        self.streams.append((reader, writer))

    def registered_streams(self):
        return list(self.streams)

    def clear_registered_streams(self):
        self.streams.clear()

    async def stream_connect(self, destination_b32):
        if self.closing:
            raise SamRuntimeClosed()
        reader, writer = await self.client.stream_connect(
            destination_b32,
            cancel_event=self.connect_cancel_event,
        )
        if self.closing:
            await self.close_stream(writer)
            raise SamRuntimeClosed()
        self.register_stream(reader, writer)
        return reader, writer

    async def stream_accept(self):
        if self.closing:
            raise SamRuntimeClosed()
        reader, writer = await self.client.stream_accept(cancel_event=self.accept_cancel_event)
        if self.closing:
            await self.close_stream(writer)
            raise SamRuntimeClosed()
        self.register_stream(reader, writer)
        return reader, writer

    async def close_stream(self, writer):
        if writer is None:
            return
        try:
            if not writer.is_closing():
                try:
                    await writer.drain()
                except:
                    pass
                writer.close()
            await writer.wait_closed()
        except:
            pass

    async def wait_for_tasks(self):
        tasks = list(self.accept_tasks | self.connect_tasks | self.send_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close_registered_streams(self, exclude_writers=None):
        exclude_writers = exclude_writers or set()
        for _, writer in self.registered_streams():
            if writer in exclude_writers:
                continue
            await self.close_stream(writer)
        self.clear_registered_streams()

    async def close_client_after_grace(self):
        await asyncio.sleep(SAM_CONNECT_CANCEL_GRACE)
        client = self.closing_client or self.client
        try:
            await client.close()
        except:
            pass
        self.closing_client = None
