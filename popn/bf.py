"""The client's payload cipher: standard Blowfish, ECB, big-endian blocks.

Only four messages use it:
    0x4007 login, 0x4100 create character, 0x4001 (dead)   client -> server
    0x4006 issued account                                     server -> client
The whole payload is zero-padded to a multiple of 8 and enciphered; the frame MD5 is
computed over the ciphertext, so wire.py needs no changes.

Key: 56 bytes in the client at 0x311dd8. Blowfish's P-array and S-boxes are the hexadecimal
digits of pi (the client uses the standard tables), so they are computed here rather
than shipped: the image stays stdlib-only. E(0) must equal
13caa3a68c4fe6bf, the repeating block seen in enciphered zero padding.
"""
import struct

KEY = bytes.fromhex(
    "017f5642869a6b827519efd00273145f2bc992ac239391da8627881e72940d37"
    "63fc079a14089196843bbc76280a1f39655cdd217b98183c")

_N_WORDS = 18 + 4 * 256
_M32 = 0xFFFFFFFF


def _pi_words(n):
    """The first n 32-bit words of the fractional hex digits of pi (Machin's formula)."""
    bits = n * 32
    guard = 64
    one = 1 << (bits + guard)

    def arctan_inv(x):
        x2 = x * x
        term = one // x
        total, k, sign = term, 1, -1
        while term:
            term //= x2
            k += 2
            total += sign * (term // k)
            sign = -sign
        return total

    pi = 4 * (4 * arctan_inv(5) - arctan_inv(239))
    frac = (pi - 3 * one) >> guard
    return [(frac >> (bits - 32 * (i + 1))) & _M32 for i in range(n)]


class Blowfish:
    def __init__(self, key):
        words = _pi_words(_N_WORDS)
        self.P = words[:18]
        self.S = [words[18 + 256 * i:18 + 256 * (i + 1)] for i in range(4)]
        k = len(key)
        j = 0
        for i in range(18):
            d = 0
            for _ in range(4):
                d = (d << 8) | key[j]
                j = (j + 1) % k
            self.P[i] ^= d
        left = right = 0
        for i in range(0, 18, 2):
            left, right = self._enc(left, right)
            self.P[i], self.P[i + 1] = left, right
        for box in self.S:
            for i in range(0, 256, 2):
                left, right = self._enc(left, right)
                box[i], box[i + 1] = left, right

    def _f(self, x):
        s = self.S
        h = (s[0][x >> 24] + s[1][(x >> 16) & 0xFF]) & _M32
        return ((h ^ s[2][(x >> 8) & 0xFF]) + s[3][x & 0xFF]) & _M32

    def _enc(self, left, right):
        p = self.P
        for i in range(16):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[17], right ^ p[16]

    def _dec(self, left, right):
        p = self.P
        for i in range(17, 1, -1):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[0], right ^ p[1]

    def _run(self, data, fn):
        if len(data) % 8:
            raise ValueError(f"Blowfish-ECB needs a multiple of 8 bytes, got {len(data)}")
        out = bytearray()
        for i in range(0, len(data), 8):
            left, right = struct.unpack(">II", data[i:i + 8])
            out += struct.pack(">II", *fn(left, right))
        return bytes(out)

    def encrypt(self, data):
        return self._run(data, self._enc)

    def decrypt(self, data):
        return self._run(data, self._dec)


_CIPHER = None


def _cipher():
    global _CIPHER
    if _CIPHER is None:
        _CIPHER = Blowfish(KEY)
    return _CIPHER


def pad8(data):
    """Zero-pad to a multiple of 8, as the client does before enciphering."""
    return data + b"\0" * (-len(data) % 8)


def encrypt_payload(plain):
    """Server -> client (0x4006): pad, then encipher."""
    return _cipher().encrypt(pad8(plain))


def decrypt_payload(payload):
    """Client -> server (0x4007/0x4100/0x4001). The client always sends a multiple of 8;
    anything else is treated as already-plaintext (old self-tests, hand-made frames)."""
    if not payload or len(payload) % 8:
        return payload
    return _cipher().decrypt(payload)
