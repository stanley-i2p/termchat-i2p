import asyncio
import i2plib.sam
import i2plib.exceptions
from i2plib.log import logger


def parse_reply(data):
    if not data:
        raise ConnectionAbortedError("Empty SAM response")

    msg = i2plib.sam.Message(data.decode().strip())
    logger.debug("SAM reply: %s", msg)

    return msg


async def get_sam_socket(sam_address=i2plib.sam.DEFAULT_ADDRESS):
    reader, writer = await asyncio.open_connection(*sam_address)

    writer.write(i2plib.sam.hello("3.1", "3.1"))
    await writer.drain()

    reply = parse_reply(await reader.readline())

    if reply.ok:
        return reader, writer

    writer.close()
    await writer.wait_closed()
    raise i2plib.exceptions.SAM_EXCEPTIONS[reply["RESULT"]]()
    

async def dest_lookup(domain, sam_address=i2plib.sam.DEFAULT_ADDRESS):

    reader, writer = await get_sam_socket(sam_address)

    writer.write(i2plib.sam.naming_lookup(domain))
    await writer.drain()

    reply = parse_reply(await reader.readline())

    writer.close()
    await writer.wait_closed()

    if reply.ok:
        return i2plib.sam.Destination(reply["VALUE"])

    raise i2plib.exceptions.SAM_EXCEPTIONS[reply["RESULT"]]()
    

async def new_destination(
        sam_address=i2plib.sam.DEFAULT_ADDRESS,
        sig_type=i2plib.sam.Destination.default_sig_type):

    reader, writer = await get_sam_socket(sam_address)

    writer.write(i2plib.sam.dest_generate(sig_type))
    await writer.drain()

    reply = parse_reply(await reader.readline())

    writer.close()
    await writer.wait_closed()

    return i2plib.sam.Destination(reply["PRIV"], has_private_key=True)


async def create_session(
        session_name,
        sam_address=i2plib.sam.DEFAULT_ADDRESS,
        style="STREAM",
        signature_type=i2plib.sam.Destination.default_sig_type,
        destination=None,
        options=None):

    options = options or {}

    if destination:
        if not isinstance(destination, i2plib.sam.Destination):
            destination = i2plib.sam.Destination(destination, has_private_key=True)

        dest_string = destination.private_key.base64

    else:
        dest_string = i2plib.sam.TRANSIENT_DESTINATION

    options = " ".join(f"{k}={v}" for k, v in options.items())

    reader, writer = await get_sam_socket(sam_address)

    writer.write(
        i2plib.sam.session_create(style, session_name, dest_string, options)
    )

    await writer.drain()

    reply = parse_reply(await reader.readline())

    if reply.ok:

        if not destination:
            destination = i2plib.sam.Destination(
                reply["DESTINATION"], has_private_key=True
            )

        logger.debug("Session created %s", session_name)

        return reader, writer

    writer.close()
    await writer.wait_closed()

    raise i2plib.exceptions.SAM_EXCEPTIONS[reply["RESULT"]]()
    

async def stream_connect(
        session_name,
        destination,
        sam_address=i2plib.sam.DEFAULT_ADDRESS):

    logger.debug("Connecting stream %s", session_name)

    if isinstance(destination, str) and destination.endswith(".i2p"):
        destination = await dest_lookup(destination, sam_address)

    elif isinstance(destination, str):
        destination = i2plib.sam.Destination(destination)

    reader, writer = await get_sam_socket(sam_address)

    writer.write(
        i2plib.sam.stream_connect(session_name, destination.base64, silent="false")
    )

    await writer.drain()

    reply = parse_reply(await reader.readline())

    if reply.ok:
        return reader, writer

    writer.close()
    await writer.wait_closed()

    raise i2plib.exceptions.SAM_EXCEPTIONS[reply["RESULT"]]()
    

async def stream_accept(session_name, sam_address=i2plib.sam.DEFAULT_ADDRESS):

    reader, writer = await get_sam_socket(sam_address)

    writer.write(i2plib.sam.stream_accept(session_name, silent="false"))
    await writer.drain()

    reply = parse_reply(await reader.readline())

    if reply.ok:
        return reader, writer

    writer.close()
    await writer.wait_closed()

    raise i2plib.exceptions.SAM_EXCEPTIONS[reply["RESULT"]]()


class Session:
    def __init__(self, session_name, sam_address=i2plib.sam.DEFAULT_ADDRESS,
                 style="STREAM", destination=None, options=None):

        self.session_name = session_name
        self.sam_address = sam_address
        self.style = style
        self.destination = destination
        self.options = options or {}

    async def __aenter__(self):

        self.reader, self.writer = await create_session(
            self.session_name,
            sam_address=self.sam_address,
            style=self.style,
            destination=self.destination,
            options=self.options
        )

        return self

    async def __aexit__(self, exc_type, exc, tb):

        self.writer.close()
        await self.writer.wait_closed()


class StreamConnection:

    def __init__(self, session_name, destination,
                 sam_address=i2plib.sam.DEFAULT_ADDRESS):

        self.session_name = session_name
        self.destination = destination
        self.sam_address = sam_address

    async def __aenter__(self):

        self.reader, self.writer = await stream_connect(
            self.session_name,
            self.destination,
            sam_address=self.sam_address
        )

        self.read = self.reader.read
        self.write = self.writer.write

        return self

    async def __aexit__(self, exc_type, exc, tb):

        self.writer.close()
        await self.writer.wait_closed()


class StreamAcceptor:

    def __init__(self, session_name,
                 sam_address=i2plib.sam.DEFAULT_ADDRESS):

        self.session_name = session_name
        self.sam_address = sam_address

    async def __aenter__(self):

        self.reader, self.writer = await stream_accept(
            self.session_name,
            sam_address=self.sam_address
        )

        self.read = self.reader.read
        self.write = self.writer.write

        return self

    async def __aexit__(self, exc_type, exc, tb):

        self.writer.close()
        await self.writer.wait_closed()
