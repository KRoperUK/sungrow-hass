"""Encrypted Modbus TCP for SG-D series inverters and newer dongles (#574).

A plain WiNet-S dongle speaks ordinary Modbus TCP. Some units — the SG-D series
(SG8K-D and siblings) and newer dongles — wrap every frame in an AES-128-ECB
envelope instead. The plaintext is a normal Modbus TCP frame with its transaction
id replaced by the magic header ``0x6868``, then padded to a 16-byte boundary with
``0xFF``. The wire frame is::

    [0x01, 0x00, length, padding] + AES_ECB(0x6868 + frame[2:] + 0xFF * padding)

The session key is negotiated per connection, and the dongle hands out a fresh
public key each day:

1. Send the plain *key request* ``GET_KEY`` (unit 0xF7, FC4, address 0x0AE7, 8
   registers).
2. The reply is 25 bytes: a 9-byte header followed by a 16-byte public key.
3. ``key = public_key XOR PRIV_KEY``. An all-zero or all-``0xFF`` public key means the
   dongle does not encrypt, and traffic stays plain.
4. The dongle expects a fresh TCP connection after issuing a key, so reconnect before
   the first encrypted request.

Derived from three independent implementations that agree on every constant and on the
framing rules: rpvelloso/Sungrow-Modbus (MIT), switter02/sungrow-ha (GPL-3.0) and
Cyber40014/sungrow-solarcloud-haos-integration. Only ``cryptography`` (shipped with
Home Assistant) is used for AES.

The envelope stores the frame length in a single byte, so an encrypted reply must stay
within 255 bytes — a read of 120 registers (``9 + 2 * 120 = 249``) is the practical
ceiling, comfortably under the 100-register blocks the integration already reads.

Negotiation is opt-in: :class:`SungrowModbusClient` enables it only after a plain read
fails, so a dongle that already speaks plain Modbus never sees a handshake.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pymodbus.client import AsyncModbusTcpClient

_LOGGER = logging.getLogger(__name__)

# Vendor constant, recovered from the dongle firmware; identical in every independent
# implementation we cross-checked.
PRIV_KEY = b"Grow#0*2Sun68CbE"
# A dongle that does not encrypt answers the key request with one of these sentinels.
NO_CRYPTO_ZERO = b"\x00" * 16
NO_CRYPTO_ONES = b"\xff" * 16
# Plain key request: unit 0xF7, FC4, address 0x0AE7, 8 registers.
GET_KEY = b"\x68\x68\x00\x00\x00\x06\xf7\x04\x0a\xe7\x00\x08"
# 9-byte header + 16-byte public key.
KEY_REPLY_LEN = 25
HEADER = b"\x68\x68"
HANDSHAKE_TIMEOUT = 5.0


def is_encrypted_key(public_key: bytes) -> bool:
    """Return True when a key reply means the dongle encrypts its Modbus traffic."""
    return len(public_key) == 16 and public_key not in (NO_CRYPTO_ZERO, NO_CRYPTO_ONES)


def derive_key(public_key: bytes) -> bytes:
    """Derive the AES-128 session key from the dongle's public key."""
    return bytes(a ^ b for a, b in zip(public_key, PRIV_KEY, strict=True))


def make_cipher(key: bytes) -> Cipher[Any]:
    """Build the AES-128-ECB cipher the dongle's protocol requires.

    ECB is dictated by the dongle firmware: it encrypts fixed 16-byte blocks with no IV
    or chaining, so no other mode is interoperable. This is not a local design choice and
    cannot be strengthened without breaking the device; CodeQL's
    ``py/weak-cryptographic-algorithm`` alert on it is a known false positive for this
    protocol.
    """
    return Cipher(algorithms.AES(key), modes.ECB())


def seal(frame: bytes, cipher: Cipher[Any]) -> bytes:
    """Wrap one Modbus TCP frame in the Sungrow AES envelope."""
    padding = 16 - (len(frame) % 16)
    plain = HEADER + frame[2:] + b"\xff" * padding
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plain) + encryptor.finalize()
    return bytes((0x01, 0x00, len(frame), padding)) + ciphertext


def open_sealed(envelope: bytes, cipher: Cipher[Any], transaction_id: bytes) -> bytes:
    """Unwrap one ``[1, 0, length, padding] + ciphertext`` envelope into a frame.

    The dongle puts the ``0x6868`` header where the transaction id belongs, so the
    caller's own transaction id is restored before the frame reaches the framer.
    """
    frame_length = envelope[2]
    decryptor = cipher.decryptor()
    plain = decryptor.update(envelope[4:]) + decryptor.finalize()
    return (transaction_id + plain[2:])[:frame_length]


class SungrowEncryptedTcpClient(AsyncModbusTcpClient):
    """``AsyncModbusTcpClient`` that can speak the SG-D AES envelope.

    Pymodbus resolves both byte-stream seams at call time — outgoing frames through
    ``ctx.low_level_send`` and incoming bytes through ``ctx.callback_data`` — so
    replacing those two attributes wraps the whole transport without touching the
    request/response layer, retries or the framer.

    Until a session key is negotiated traffic is plain, which lets the handshake itself
    (and a non-encrypting dongle) run through the same object.
    """

    def __init__(self, host: str, *, encrypted: bool = False, **kwargs: Any) -> None:
        """Build the client; ``encrypted`` asks the next connect to negotiate a key."""
        super().__init__(host, **kwargs)
        self._host = host
        self._raw_send = self.ctx.low_level_send
        self._raw_receive = self.ctx.callback_data
        self._requested_encryption = encrypted
        self._cipher: Cipher[Any] | None = None
        self._transaction_id = b"\x00\x00"
        self._rx_fifo = b""
        self._handshake_buf = b""
        self._handshake_future: asyncio.Future[bytes] | None = None
        # Pymodbus resolves both byte-stream seams through ``ctx`` at call time, so
        # replacing them wraps the transport without touching the request layer.
        self.ctx.low_level_send = self._send
        self.ctx.callback_data = self._receive  # type: ignore[method-assign]

    @property
    def encrypted(self) -> bool:
        """Return True once a real session key has been negotiated."""
        return self._cipher is not None

    async def connect(self) -> bool:
        """Connect, negotiating the session key first when encryption was requested.

        A key is negotiated on every *new* socket rather than once per client object.
        The dongle issues a per-connection key, and WiNet-S drops idle sockets, so
        reusing the key from a dead session would encrypt every frame with a stale
        cipher and the dongle would reject it. Renegotiating on reconnect is harmless
        if a key turns out to be durable, and required if it is not.
        """
        was_active = self.connected
        if not await super().connect():
            return False
        if not self._requested_encryption:
            return True
        if self._cipher is not None and was_active:
            # Same live socket: the existing key is still in force.
            return True
        # No key yet, or the previous socket died and its key with it.
        self._cipher = None
        self._reset_buffers()
        await self._async_negotiate()
        if self._cipher is None:
            return True
        # The dongle expects a fresh socket after handing out a key.
        self.ctx.close()
        self._reset_buffers()
        return await super().connect()

    def close(self) -> None:
        """Close the socket and drop the session key."""
        super().close()
        self._cipher = None
        self._reset_buffers()

    async def _async_negotiate(self) -> None:
        """Send the key request and derive the session key from the reply."""
        self._handshake_future = asyncio.get_running_loop().create_future()
        self._handshake_buf = b""
        try:
            self._raw_send(GET_KEY)
            public_key: bytes | None = await asyncio.wait_for(self._handshake_future, HANDSHAKE_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("No key reply from %s; treating Modbus as unencrypted", self._host)
            public_key = None
        finally:
            self._handshake_future = None
            self._handshake_buf = b""
        if public_key is not None and is_encrypted_key(public_key):
            self._cipher = make_cipher(derive_key(public_key))
            _LOGGER.debug("Modbus encryption negotiated with %s", self._host)
        else:
            _LOGGER.debug("Modbus to %s is not encrypted", self._host)

    def _send(self, data: bytes, addr: tuple[Any, ...] | None = None) -> None:
        """Wrap an outgoing frame when a session key is active, else pass it through."""
        if self._cipher is None:
            self._raw_send(data, addr)
            return
        self._transaction_id = data[:2]
        self._raw_send(seal(data, self._cipher), addr)

    def _receive(self, data: bytes, addr: tuple[Any, ...] | None = None) -> int:
        """Collect the key reply, or unwrap and dispatch encrypted frames."""
        if self._handshake_future is not None:
            self._handshake_buf += data
            if len(self._handshake_buf) >= KEY_REPLY_LEN and not self._handshake_future.done():
                self._handshake_future.set_result(self._handshake_buf[9:KEY_REPLY_LEN])
            return len(data)
        if self._cipher is None:
            return self._raw_receive(data, addr)
        self._rx_fifo += data
        while len(self._rx_fifo) >= 4:
            total = self._rx_fifo[2] + self._rx_fifo[3]
            if total == 0 or total % 16:
                _LOGGER.warning("Malformed encrypted Modbus envelope from %s; discarding", self._host)
                self._rx_fifo = b""
                return len(data)
            if len(self._rx_fifo) < 4 + total:
                break
            envelope, self._rx_fifo = self._rx_fifo[: 4 + total], self._rx_fifo[4 + total :]
            self._raw_receive(open_sealed(envelope, self._cipher, self._transaction_id), addr)
        return len(data)

    def _reset_buffers(self) -> None:
        self._rx_fifo = b""
        self._transaction_id = b"\x00\x00"
