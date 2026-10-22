"""The Konami frame, as SLPM_624.64 speaks it.

    whole frame XOR KEY[i & 3], i counted from the first header byte
    header, 24 bytes, big-endian:
        +0x00 u16 opcode
        +0x02 u16 payload length, 0..0x400
        +0x04 u32 serial, per connection and direction, incremented per send
        +0x08 16  MD5(header[0:8] || payload)
    payload

The client CHECKS the MD5 of what we send it (failure -0xd1), so every frame must
digest correctly.
"""
import hashlib
import struct

KEY = bytes.fromhex("aaa55a55")  # client 0x2cda20
HDR = 24
MAX_PAYLOAD = 0x400
GOODBYE = 0x0003


class FrameError(Exception):
    pass


def xor(buf, key=KEY):
    return bytes(b ^ key[i & 3] for i, b in enumerate(buf))


def digest(hdr8, payload):
    return hashlib.md5(hdr8 + payload).digest()


def build(opcode, serial, payload=b"", key=KEY):
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload {len(payload)} > 0x400")
    hdr8 = struct.pack(">HHI", opcode, len(payload), serial & 0xFFFFFFFF)
    return xor(hdr8 + digest(hdr8, payload) + payload, key)


class Frame:
    __slots__ = ("opcode", "serial", "payload", "digest_ok")

    def __init__(self, opcode, serial, payload, digest_ok):
        self.opcode, self.serial, self.payload, self.digest_ok = opcode, serial, payload, digest_ok

    def __repr__(self):
        ok = "" if self.digest_ok else " BAD-MD5"
        return f"<{self.opcode:#06x} serial={self.serial} len={len(self.payload)}{ok}>"


class Decoder:
    """Feed raw bytes from the socket, get complete frames back."""

    def __init__(self, key=KEY):
        self.key = key
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= HDR:
            hdr = xor(bytes(self.buf[:HDR]), self.key)
            opcode, length, serial = struct.unpack(">HHI", hdr[:8])
            if length > MAX_PAYLOAD:
                raise FrameError(f"length {length:#x} > 0x400 (op {opcode:#06x})")
            if len(self.buf) < HDR + length:
                break
            # The payload restarts at key[0]: 24 % 4 == 0.
            payload = xor(bytes(self.buf[HDR:HDR + length]), self.key)
            del self.buf[:HDR + length]
            out.append(Frame(opcode, serial, payload, digest(hdr[:8], payload) == hdr[8:HDR]))
        return out


class Writer:
    """Payload builder mirroring the client's codecs (all big-endian)."""

    def __init__(self):
        self.b = bytearray()

    def u8(self, v):
        self.b += struct.pack(">B", v & 0xFF)
        return self

    def u16(self, v):
        self.b += struct.pack(">H", v & 0xFFFF)
        return self

    def u32(self, v):
        self.b += struct.pack(">I", v & 0xFFFFFFFF)
        return self

    def fixed(self, data, n):
        """Exactly n bytes, NUL-padded or truncated (as the client reads it)."""
        if isinstance(data, str):
            data = data.encode("cp932")
        self.b += data[:n].ljust(n, b"\0")
        return self

    def string(self, s, delim=0):
        """String as the client reads it: stops at `delim` or NUL, which it consumes."""
        if isinstance(s, str):
            s = s.encode("cp932")
        self.b += s + bytes([delim])
        return self

    def bytes(self):
        if len(self.b) > MAX_PAYLOAD:
            raise FrameError(f"payload {len(self.b)} > 0x400")
        return bytes(self.b)


class Reader:
    """Payload parser mirroring the client's writers."""

    def __init__(self, payload):
        self.p = payload
        self.i = 0

    def _take(self, n):
        if self.i + n > len(self.p):
            raise FrameError(f"read {n} at {self.i} past end {len(self.p)}")
        v = self.p[self.i:self.i + n]
        self.i += n
        return v

    def u8(self):
        return self._take(1)[0]

    def u32(self):
        return struct.unpack(">I", self._take(4))[0]

    def fixed(self, n):
        return self._take(n)

    def cstr(self, n):
        """A fixed n-byte field holding a NUL-terminated string."""
        return self._take(n).split(b"\0", 1)[0].decode("cp932", "replace")

    def rest(self):
        return self._take(len(self.p) - self.i)
