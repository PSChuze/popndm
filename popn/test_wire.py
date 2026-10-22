"""Self-test for the frame codec and the 5731 stub.

    python -m unittest popn.test_wire -v            (from the repo root)

Set POPN_ELF to the disc's SLPM_624.64 to also check the constants against the client.

ClientModel is written from the client's observed behaviour (receive and serial
check), NOT from wire.py, so the two check each other.
"""
import hashlib
import os
import socket
import struct
import threading
import unittest

from . import gate, wire

ELF = os.environ.get("POPN_ELF")


class ClientModel:
    """The client's receive side, step for step."""

    def __init__(self, key):
        self.key = key
        self.expect = 0  # the next serial the client accepts

    def recv(self, raw):
        hdr = bytes(b ^ self.key[i % 4] for i, b in enumerate(raw[:0x18]))
        opcode = hdr[0] * 0x100 + hdr[1]
        length = hdr[2] * 0x100 + hdr[3]
        serial = (hdr[4] << 24) + (hdr[5] << 16) + (hdr[6] << 8) + hdr[7]
        if length > 0x400:
            return "-0xce"
        body = bytes(b ^ self.key[(i + 0x18) % 4] for i, b in enumerate(raw[0x18:0x18 + length]))
        if hashlib.md5(hdr[:8] + body).digest() != hdr[8:0x18]:
            return "-0xd1"
        self.expect += 1
        if serial != self.expect:
            return "-0x49"
        return opcode, body


class CodecTest(unittest.TestCase):
    def test_roundtrip(self):
        for n in (0, 1, 3, 4, 0x3FF, 0x400):
            p = bytes(range(256)) * 4
            raw = wire.build(0x4004, 7, p[:n])
            (f,) = wire.Decoder().feed(raw)
            self.assertEqual((f.opcode, f.serial, f.payload, f.digest_ok), (0x4004, 7, p[:n], True))

    def test_client_accepts_ours(self):
        c = ClientModel(wire.KEY)
        for s in (1, 2, 3):
            self.assertEqual(c.recv(wire.build(0x4004, s, b"x" * s)), (0x4004, b"x" * s))

    def test_client_rejects_bad_serial_and_digest(self):
        c = ClientModel(wire.KEY)
        self.assertEqual(c.recv(wire.build(0x4004, 2)), "-0x49")
        raw = bytearray(wire.build(0x4004, 1, b"abcd"))
        raw[-1] ^= 1
        self.assertEqual(ClientModel(wire.KEY).recv(bytes(raw)), "-0xd1")

    def test_oversize(self):
        with self.assertRaises(wire.FrameError):
            wire.build(1, 1, b"\0" * 0x401)

    def test_split_feed(self):
        raw = wire.build(0x4003, 1) + wire.build(0x4007, 2, b"\1" + b"\0" * 0x110)
        d, got = wire.Decoder(), []
        for i in range(len(raw)):
            got += d.feed(raw[i:i + 1])
        self.assertEqual([f.opcode for f in got], [0x4003, 0x4007])

    def test_writer_reader(self):
        p = wire.Writer().u8(1).fixed("popn", 0x100).fixed(b"\xaa" * 16, 16).u32(0x12345678).bytes()
        r = wire.Reader(p)
        self.assertEqual((r.u8(), r.cstr(0x100), r.fixed(16), r.u32()), (1, "popn", b"\xaa" * 16, 0x12345678))

    def test_login_digest(self):
        chal = bytes(range(16))
        self.assertEqual(gate.login_digest("abc", chal, "pw"), hashlib.md5(b"abc" + chal + b"pw").digest())


class GateTest(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("POPN_AUTH_DB", "POPN_AUTH_OPEN")}
        self.db = os.path.join(os.path.dirname(__file__), "..", "data", "test-accounts.json")
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        with open(self.db, "w", encoding="utf-8") as f:
            f.write('{"alice": "pw"}')
        os.environ["POPN_AUTH_DB"] = self.db
        os.environ.pop("POPN_AUTH_OPEN", None)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_check_login(self):
        chal = os.urandom(16)
        good = {"id": "alice", "digest": gate.login_digest("alice", chal, "pw")}
        bad = {"id": "alice", "digest": gate.login_digest("alice", chal, "nope")}
        stranger = {"id": "who", "digest": b"\0" * 16}
        acc = {"alice": "pw"}
        self.assertTrue(gate.check_login(good, chal, acc, False))
        self.assertFalse(gate.check_login(bad, chal, acc, True))
        self.assertFalse(gate.check_login(stranger, chal, acc, False))
        self.assertTrue(gate.check_login(stranger, chal, acc, True))

    def test_challenge_then_login(self):
        a, b = socket.socketpair()
        logs = os.path.join(os.path.dirname(__file__), "..", "data", "test-logs")
        os.makedirs(logs, exist_ok=True)
        t = threading.Thread(target=gate.Session(b, ("test", 0), logs).run, daemon=True)
        t.start()
        a.sendall(wire.build(0x4003, 1))
        raw = b""
        while len(raw) < wire.HDR + 16:
            raw += a.recv(4096)
        self.client = ClientModel(wire.KEY)
        op, chal = self.client.recv(raw)
        self.assertEqual((op, len(chal)), (0x4004, 16))
        body = wire.Writer().u8(1).fixed("alice", 0x100).fixed(gate.login_digest("alice", chal, "pw"), 16)
        a.sendall(wire.build(0x4007, 2, body.bytes()))
        raw = b""
        while len(raw) < wire.HDR + 4:
            raw += a.recv(4096)
        self.assertEqual(self.client.recv(raw), (0x4008, b"\0\0\0\0"))
        a.sendall(wire.build(0x0003, 3))
        t.join(15)
        self.assertFalse(t.is_alive())
        a.close()


@unittest.skipUnless(ELF, "set POPN_ELF to SLPM_624.64")
class DiscTest(unittest.TestCase):
    """The constants in wire.py, read back out of the client."""

    def setUp(self):
        with open(ELF, "rb") as f:
            self.d = f.read()

    def va(self, a, n):
        o = a - 0x100000 + 0x80
        return self.d[o:o + n]

    def test_key(self):
        self.assertEqual(self.va(0x2CDA20, 4), wire.KEY)

    def test_server_table(self):
        host, port = struct.unpack("<II", self.va(0x2CDA70, 8))
        self.assertEqual(self.va(host, 25), b"ps2pzd1.konamionline.com\0")
        self.assertEqual(port, 5731)

    def test_login_format(self):
        self.assertEqual(self.va(0x31E188, 3), b"%s\0")


if __name__ == "__main__":
    unittest.main()
