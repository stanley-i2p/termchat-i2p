import logging
import asyncio
import argparse

import i2plib.sam
import i2plib.aiosam
import i2plib.utils
from i2plib.log import logger

BUFFER_SIZE = 65536


async def proxy_data(reader, writer):
    """Proxy data from reader to writer"""
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break

            writer.write(data)
            await writer.drain()

    except Exception as e:
        logger.debug(f'proxy_data exception {e}')

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        logger.debug("close connection")


class I2PTunnel:
    """Base I2P Tunnel"""

    def __init__(self, local_address, destination=None,
                 session_name=None, options=None,
                 sam_address=i2plib.sam.DEFAULT_ADDRESS):

        self.local_address = local_address
        self.destination = destination
        self.session_name = session_name or i2plib.utils.generate_session_id()
        self.options = options or {}
        self.sam_address = sam_address

    async def _pre_run(self):

        if not self.destination:
            self.destination = await i2plib.aiosam.new_destination(
                sam_address=self.sam_address
            )

        _, self.session_writer = await i2plib.aiosam.create_session(
            self.session_name,
            style=self.style,
            options=self.options,
            sam_address=self.sam_address,
            destination=self.destination
        )

    def stop(self):
        if hasattr(self, "session_writer"):
            self.session_writer.close()


class ClientTunnel(I2PTunnel):

    def __init__(self, remote_destination, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style = "STREAM"
        self.remote_destination = remote_destination

    async def run(self):

        await self._pre_run()

        async def handle_client(client_reader, client_writer):

            remote_reader, remote_writer = await i2plib.aiosam.stream_connect(
                self.session_name,
                self.remote_destination,
                sam_address=self.sam_address
            )

            asyncio.create_task(proxy_data(remote_reader, client_writer))
            asyncio.create_task(proxy_data(client_reader, remote_writer))

        self.server = await asyncio.start_server(
            handle_client,
            *self.local_address
        )

    def stop(self):
        super().stop()
        if hasattr(self, "server"):
            self.server.close()


class ServerTunnel(I2PTunnel):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style = "STREAM"

    async def run(self):

        await self._pre_run()

        async def handle_client(incoming, client_reader, client_writer):

            dest, data = incoming.split(b"\n", 1)

            remote_destination = i2plib.sam.Destination(dest.decode())

            logger.debug(
                f"{self.session_name} client connected: {remote_destination.base32}.b32.i2p"
            )

            try:

                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=self.local_address[0],
                        port=self.local_address[1]
                    ),
                    timeout=5
                )

                if data:
                    remote_writer.write(data)
                    await remote_writer.drain()

                asyncio.create_task(proxy_data(remote_reader, client_writer))
                asyncio.create_task(proxy_data(client_reader, remote_writer))

            except ConnectionRefusedError:
                client_writer.close()

        async def server_loop():

            try:
                while True:

                    client_reader, client_writer = await i2plib.aiosam.stream_accept(
                        self.session_name,
                        sam_address=self.sam_address
                    )

                    incoming = await client_reader.read(BUFFER_SIZE)

                    asyncio.create_task(
                        handle_client(incoming, client_reader, client_writer)
                    )

            except asyncio.CancelledError:
                pass

        self.server_loop = asyncio.create_task(server_loop())

    def stop(self):
        super().stop()

        if hasattr(self, "server_loop"):
            self.server_loop.cancel()


# CLI runner

async def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "type",
        metavar="TYPE",
        choices=("server", "client"),
        help="Tunnel type"
    )

    parser.add_argument(
        "address",
        metavar="ADDRESS",
        help="Local address (127.0.0.1:8000)"
    )

    parser.add_argument("--debug", "-d", action="store_true")

    parser.add_argument("--key", "-k", default="")

    parser.add_argument("--destination", "-D", default="")

    args = parser.parse_args()

    SAM_ADDRESS = i2plib.utils.get_sam_address()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO
    )

    if args.key:
        destination = i2plib.sam.Destination(
            path=args.key,
            has_private_key=True
        )
    else:
        destination = None

    local_address = i2plib.utils.address_from_string(args.address)

    if args.type == "client":

        tunnel = ClientTunnel(
            args.destination,
            local_address,
            destination=destination,
            sam_address=SAM_ADDRESS
        )

    else:

        tunnel = ServerTunnel(
            local_address,
            destination=destination,
            sam_address=SAM_ADDRESS
        )

    await tunnel.run()

    try:
        await asyncio.Future()  # run forever
    except asyncio.CancelledError:
        tunnel.stop()


if __name__ == "__main__":
    asyncio.run(main())
