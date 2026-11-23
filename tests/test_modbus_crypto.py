"""Tests for the SG-D encrypted Modbus transport (#574).

Covers the pure AES envelope helpers, the handshake/key derivation, and a full
round-trip of the client against a loopback server that speaks the dongle's protocol.
The wire format and constants are cross-checked against three independent
implementations (rpvelloso/Sungrow-Modbus, switter02/sungrow-ha,
Cyber40014/sungrow-solarcloud-haos-integration).
"""

from __future__ import annotations

import asyncio
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher

from custom_components.sungrow.modbus import SungrowModbusClient
from custom_components.sungrow.modbus_crypto import (
    GET_KEY,
    HEADER,
    KEY_REPLY_LEN,
    NO_CRYPTO_ONES,
    NO_CRYPTO_ZERO,
    PRIV_KEY,
    SungrowEncryptedTcpClient,
    derive_key,
    is_encrypted_key,
    make_cipher,
    open_sealed,
    seal,
)

PUBLIC_KEY = bytes(range(16))


def _cipher(public_key: bytes = PUBLIC_KEY) -> Cipher:
    """Build the dongle's AES cipher for the given public key."""
    return make_cipher(derive_key(public_key))


# ---------------------------------------------------------------------------
# Key negotiation
# ---------------------------------------------------------------------------


def test_derive_key_xors_public_key_with_vendor_constant():
    """The session key is the dongle's public key XOR the vendor private constant."""
    assert derive_key(b"\x00" * 16) == PRIV_KEY
    assert derive_key(PRIV_KEY) == b"\x00" * 16
    assert len(derive_key(PUBLIC_KEY)) == 16


@pytest.mark.parametrize("sentinel", [NO_CRYPTO_ZERO, NO_CRYPTO_ONES])
def test_is_encrypted_key_rejects_no_crypto_sentinels(sentinel):
    """All-zero / all-0xFF key replies mean the dongle does not encrypt."""
    assert is_encrypted_key(sentinel) is False


def test_is_encrypted_key_accepts_a_real_key():
    """A 16-byte key that is neither sentinel means encryption is active."""
    assert is_encrypted_key(PUBLIC_KEY) is True


@pytest.mark.parametrize("bad", [b"", b"\x01" * 15, b"\x01" * 17])
def test_is_encrypted_key_rejects_wrong_length(bad):
    """Only a 16-byte key can be used for AES-128."""
    assert is_encrypted_key(bad) is False


# ---------------------------------------------------------------------------
# AES envelope
# ---------------------------------------------------------------------------


def test_seal_wraps_frame_with_header_and_padding():
    """The envelope is ``[1, 0, length, padding]`` followed by whole AES blocks."""
    frame = b"\x00\x01\x00\x00\x00\x06\x01\x04\x13\x87\x00\x01"
    envelope = seal(frame, _cipher())

    assert envelope[0] == 1
    assert envelope[1] == 0
    assert envelope[2] == len(frame)
    padding = 16 - (len(frame) % 16)
    assert envelope[3] == padding
    # Ciphertext is a whole number of AES blocks.
    assert (len(envelope) - 4) % 16 == 0
    assert len(envelope) == 4 + len(frame) + padding


def test_seal_adds_a_full_block_when_already_aligned():
    """A 16-byte frame gets a whole extra padding block (never zero padding)."""
    frame = b"\x00\x01" + b"\x00" * 14
    envelope = seal(frame, _cipher())
    assert envelope[3] == 16
    assert len(envelope) == 4 + 32


def test_seal_round_trips_through_open_sealed():
    """Decrypting an envelope restores the frame, with the caller's transaction id."""
    frame = b"\x12\x34\x00\x00\x00\x06\x01\x04\x13\x87\x00\x01"
    cipher = _cipher()
    envelope = seal(frame, cipher)
    restored = open_sealed(envelope, cipher, frame[:2])
    assert restored == frame


def test_sealed_plaintext_starts_with_magic_header():
    """The plaintext is ``0x6868`` + the frame minus its transaction id, then 0xFF."""
    frame = b"\x12\x34\x00\x00\x00\x06\x01\x04\x13\x87\x00\x01"
    cipher = _cipher()
    envelope = seal(frame, cipher)
    decryptor = cipher.decryptor()
    plain = decryptor.update(envelope[4:]) + decryptor.finalize()
    assert plain.startswith(HEADER + frame[2:])
    assert set(plain[len(HEADER) + len(frame) - 2 :]) <= {0xFF}


def test_get_key_request_shape():
    """The key request is the documented 12-byte plain frame."""
    assert GET_KEY == b"\x68\x68\x00\x00\x00\x06\xf7\x04\x0a\xe7\x00\x08"
    assert len(GET_KEY) == 12
    assert KEY_REPLY_LEN == 25


# ---------------------------------------------------------------------------
# Client against a loopback dongle
# ---------------------------------------------------------------------------

REGISTERS = [0] * 70000
REGISTERS[4999] = 9732  # SG3.6RS device-type code
REGISTERS[5002] = 1234  # daily yield -> 123.4 kWh
REGISTERS[5035] = 499  # grid frequency -> 49.9 Hz


def _reply(frame: bytes) -> bytes:
    """Build a plain Modbus reply to one input-register read."""
    tid, dev, fc = frame[:2], frame[6], frame[7]
    _, start, count = struct.unpack(">BHH", frame[7:12])
    values = [REGISTERS[a % len(REGISTERS)] for a in range(start, start + count)]
    payload = bytes([fc, count * 2]) + struct.pack(f">{count}H", *values)
    return tid + b"\x00\x00" + (len(payload) + 1).to_bytes(2, "big") + bytes([dev]) + payload


async def _serve(encrypted: bool, handshakes: list[int] | None = None) -> asyncio.AbstractServer:
    """A loopback dongle: plain handshake, then AES envelopes (or plain frames).

    ``handshakes``, when given, has one element appended per key request received, so a
    test can assert whether a reconnect renegotiated.
    """
    cipher = _cipher()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fifo = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                fifo += chunk
                if fifo.startswith(GET_KEY):
                    # Both kinds of dongle answer the key request; only the sentinel
                    # in the reply decides whether traffic is then encrypted.
                    fifo = fifo[len(GET_KEY) :]
                    if handshakes is not None:
                        handshakes.append(1)
                    public_key = PUBLIC_KEY if encrypted else NO_CRYPTO_ZERO
                    writer.write(HEADER + b"\x00" * 7 + public_key)
                    await writer.drain()
                    continue
                if not encrypted:
                    while len(fifo) >= 8:
                        total = 6 + struct.unpack(">H", fifo[4:6])[0]
                        if len(fifo) < total:
                            break
                        frame, fifo = fifo[:total], fifo[total:]
                        writer.write(_reply(frame))
                    await writer.drain()
                    continue
                while len(fifo) >= 4:
                    total = fifo[2] + fifo[3]
                    if total == 0 or total % 16 or len(fifo) < 4 + total:
                        break
                    envelope, fifo = fifo[: 4 + total], fifo[4 + total :]
                    plain = cipher.decryptor().update(envelope[4:])
                    frame = b"\x00\x00" + plain[2 : envelope[2]]
                    reply = _reply(frame)
                    pad = 16 - (len(reply) % 16)
                    body = HEADER + reply[2:] + b"\xff" * pad
                    encryptor = cipher.encryptor()
                    writer.write(bytes((1, 0, len(reply), pad)) + encryptor.update(body))
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _read_once(server: asyncio.AbstractServer, *, encrypted: bool) -> dict:
    port = server.sockets[0].getsockname()[1]
    client = SungrowModbusClient("127.0.0.1", port=port, encrypted=encrypted)
    try:
        return await asyncio.wait_for(client.async_read_realtime(), 30)
    finally:
        client.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_encrypted_round_trip_reads_decoded_points():
    """A full poll over the AES transport decodes the same points as plain Modbus."""
    server = await _serve(encrypted=True)
    try:
        data = await _read_once(server, encrypted=True)
    finally:
        server.close()
        await server.wait_closed()

    assert data["grid_frequency"]["value"] == 49.9
    assert data["daily_yield"]["value"] == 123.4
    assert data["device_type_code"]["value"] == 9732
    assert data["grid_frequency"]["source"] == "modbus"


@pytest.mark.usefixtures("socket_enabled")
async def test_encrypted_client_reports_encryption_in_diagnostics():
    """Diagnostics record that the AES transport was used, for support triage."""
    server = await _serve(encrypted=True)
    port = server.sockets[0].getsockname()[1]
    client = SungrowModbusClient("127.0.0.1", port=port, encrypted=True)
    try:
        await asyncio.wait_for(client.async_read_realtime(), 30)
        assert client.modbus_diagnostics["encrypted"] is True
    finally:
        client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.usefixtures("socket_enabled")
async def test_plain_round_trip_is_unchanged():
    """A plain dongle still reads without any handshake (pre-#574 behaviour)."""
    server = await _serve(encrypted=False)
    try:
        data = await _read_once(server, encrypted=False)
    finally:
        server.close()
        await server.wait_closed()

    assert data["grid_frequency"]["value"] == 49.9


@pytest.mark.usefixtures("socket_enabled")
async def test_non_encrypting_dongle_stays_plain():
    """A no-crypto key reply leaves the client on plain Modbus, not broken."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fifo = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                fifo += chunk
                if fifo.startswith(GET_KEY):
                    fifo = fifo[len(GET_KEY) :]
                    writer.write(HEADER + b"\x00" * 7 + NO_CRYPTO_ZERO)
                    await writer.drain()
                    continue
                while len(fifo) >= 8:
                    total = 6 + struct.unpack(">H", fifo[4:6])[0]
                    if len(fifo) < total:
                        break
                    frame, fifo = fifo[:total], fifo[total:]
                    writer.write(_reply(frame))
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = SungrowModbusClient("127.0.0.1", port=port, encrypted=True)
    try:
        data = await asyncio.wait_for(client.async_read_realtime(), 30)
        assert data["grid_frequency"]["value"] == 49.9
        assert client._client.encrypted is False  # noqa: SLF001 — transport assertion
    finally:
        client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.usefixtures("socket_enabled")
async def test_encrypted_client_starts_plain_until_connected():
    """No session key exists before connect(), so the handshake is always explicit."""
    client = SungrowEncryptedTcpClient("127.0.0.1", encrypted=True, port=502)
    assert client.encrypted is False
    client.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_reconnect_renegotiates_the_session_key():
    """A dropped socket forces a new key on reconnect.

    The dongle issues a per-connection key and WiNet-S drops idle sockets. Reusing the
    key from the dead session would encrypt every frame with a stale cipher, and the
    dongle would reject each one — the reconnect would "succeed" and then never read.
    """
    handshakes: list[int] = []
    server = await _serve(encrypted=True, handshakes=handshakes)
    port = server.sockets[0].getsockname()[1]
    client = SungrowEncryptedTcpClient("127.0.0.1", encrypted=True, port=port, timeout=5)
    try:
        assert await client.connect() is True
        assert len(handshakes) == 1, "first connect must negotiate a key"

        # Simulate pymodbus losing the socket. The client object is reused: this is
        # what SungrowModbusClient._async_ensure_connected does on a dropped session.
        client.ctx.close()
        await asyncio.sleep(0.2)
        assert client.connected is False

        assert await client.connect() is True
        assert len(handshakes) == 2, "reconnect must negotiate a fresh key"
    finally:
        client.close()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("socket_enabled")
async def test_handshake_timeout_falls_back_to_plain():
    """A dongle that never answers the key request is treated as unencrypted."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                # Deliberately ignore the key request and every later frame.
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = SungrowEncryptedTcpClient("127.0.0.1", encrypted=True, port=port, timeout=1)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("custom_components.sungrow.modbus_crypto.HANDSHAKE_TIMEOUT", 0.2)
            assert await client.connect() is True
        assert client.encrypted is False
    finally:
        client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.usefixtures("socket_enabled")
async def test_malformed_envelope_is_discarded_not_raised():
    """A non-block-aligned envelope clears the buffer instead of wedging the client."""
    client = SungrowEncryptedTcpClient("127.0.0.1", encrypted=True, port=502)
    delivered: list[bytes] = []
    try:
        client._cipher = _cipher()  # noqa: SLF001 — exercise the RX path directly
        client._raw_receive = lambda data, addr=None: delivered.append(data)  # noqa: SLF001
        client._receive(b"\x01\x00\x05\x03garbage")  # noqa: SLF001
        assert delivered == []
        assert client._rx_fifo == b""  # noqa: SLF001
    finally:
        client.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_receive_reassembles_a_split_envelope():
    """An envelope arriving in two TCP chunks is unwrapped once it is complete."""
    client = SungrowEncryptedTcpClient("127.0.0.1", encrypted=True, port=502)
    delivered: list[bytes] = []
    try:
        frame = b"\x00\x07\x00\x00\x00\x06\x01\x04\x13\x87\x00\x01"
        cipher = _cipher()
        envelope = seal(frame, cipher)
        client._cipher = cipher  # noqa: SLF001
        client._transaction_id = frame[:2]  # noqa: SLF001
        client._raw_receive = lambda data, addr=None: delivered.append(data)  # noqa: SLF001

        client._receive(envelope[:5])  # noqa: SLF001
        assert delivered == []
        client._receive(envelope[5:])  # noqa: SLF001
        assert delivered == [frame]
    finally:
        client.close()
