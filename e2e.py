import os
from hashlib import sha256

from nacl.public import PrivateKey, PublicKey
from nacl.bindings import crypto_scalarmult
from nacl.secret import SecretBox
from nacl.utils import random


class E2E:

    def __init__(self):

        
        self.private_key = PrivateKey.generate()
        self.public_key = self.private_key.public_key

        self.peer_public = None
        self.session_key = None

    

    def public_bytes(self):
        return bytes(self.public_key)


    def receive_peer_key(self, data):

        self.peer_public = PublicKey(data)

        shared = crypto_scalarmult(
            self.private_key._private_key,
            self.peer_public._public_key
        )

        self.session_key = sha256(shared).digest()


    def ready(self):
        return self.session_key is not None



    def encrypt(self, payload):

        if not self.session_key:
            return payload

        box = SecretBox(self.session_key)
        nonce = random(24)

        return box.encrypt(payload, nonce)


    def decrypt(self, payload):

        if not self.session_key:
            return payload

        box = SecretBox(self.session_key)

        try:
            return box.decrypt(payload)
        except:
            return payload
