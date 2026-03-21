import os
from hashlib import sha256

from nacl.public import PrivateKey, PublicKey
from nacl.bindings import crypto_scalarmult
from nacl.secret import SecretBox
from nacl.utils import random


class E2E:

    def __init__(self):

        # identity keys
        self.private_key = PrivateKey.generate()
        self.public_key = self.private_key.public_key

        self.peer_public = None
        self.session_key = None


    
    # Handshake
    

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


    
    # Encryption
    

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
        
        
        
        
        
    # Offline blob helpers
    

    def derive_offline_blob_key(self, shared_secret: bytes, my_b32: str, peer_b32: str):
        low_id, high_id = sorted([my_b32.strip().lower(), peer_b32.strip().lower()])

        material = b"|".join([
            b"OFFLINE_BLOB_V1",
            shared_secret,
            low_id.encode(),
            high_id.encode(),
        ])

        return sha256(material).digest()


    def encrypt_offline_blob(self, frame: bytes, blob_key: bytes):
        box = SecretBox(blob_key)
        nonce = random(24)

        enc = box.encrypt(frame, nonce)

        
        return nonce + enc.ciphertext


    def decrypt_offline_blob(self, blob: bytes, blob_key: bytes):
        if len(blob) < 25:
            raise ValueError("Offline blob too short")

        nonce = blob[:24]
        ciphertext = blob[24:]

        box = SecretBox(blob_key)
        return box.decrypt(ciphertext, nonce)
