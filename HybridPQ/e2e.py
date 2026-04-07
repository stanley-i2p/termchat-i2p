import os
from hashlib import sha256

import oqs

from nacl.public import PrivateKey, PublicKey
from nacl.bindings import crypto_scalarmult
from nacl.secret import SecretBox
from nacl.utils import random


class E2E:

    def __init__(self, pq_enabled: bool = False):

        # Classical identity keys
        self.private_key = PrivateKey.generate()
        self.public_key = self.private_key.public_key

        self.peer_public = None

        # Combined live session key
        self.session_key = None

        # Hybrid state
        self.pq_enabled = pq_enabled
        self.classical_shared = None
        self.pq_shared = None

        # PQ KEM state
        self.pq_alg = "ML-KEM-768"
        self.pq_kem = None
        self.pq_public_key = None
        self.pq_secret_key = None

        if self.pq_enabled:
            self.pq_kem = oqs.KeyEncapsulation(self.pq_alg)
            self.pq_public_key = self.pq_kem.generate_keypair()


    
    # Handshake
    

    def public_bytes(self):
        return bytes(self.public_key)


    def finalize_session_key_if_ready(self):
        if self.classical_shared is None:
            return

        if self.pq_enabled:
            if self.pq_shared is None:
                return

            material = b"|".join([
                b"TERMCHAT_HYBRID_V1",
                self.classical_shared,
                self.pq_shared,
            ])
            self.session_key = sha256(material).digest()
        else:
            material = b"|".join([
                b"TERMCHAT_CLASSICAL_V1",
                self.classical_shared,
            ])
            self.session_key = sha256(material).digest()




    def receive_peer_key(self, data):

        self.peer_public = PublicKey(data)

        self.classical_shared = crypto_scalarmult(
            self.private_key._private_key,
            self.peer_public._public_key
        )

        self.finalize_session_key_if_ready()
        
        
        
    def pq_public_bytes(self):
        if not self.pq_enabled:
            return None
        return self.pq_public_key

    def receive_peer_pq_public(self, peer_pq_public: bytes) -> bytes:
        if not self.pq_enabled or not self.pq_kem:
            raise RuntimeError("PQ is not enabled locally")

        ciphertext, shared = self.pq_kem.encap_secret(peer_pq_public)
        self.pq_shared = shared
        self.finalize_session_key_if_ready()
        return ciphertext

    def receive_peer_pq_ciphertext(self, ciphertext: bytes):
        if not self.pq_enabled or not self.pq_kem:
            raise RuntimeError("PQ is not enabled locally")

        self.pq_shared = self.pq_kem.decap_secret(ciphertext)
        self.finalize_session_key_if_ready()
        


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
