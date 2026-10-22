"""Tests for the cipher, the record layouts, and a full two-player flow through the gate.

    python -m unittest popn.test_lobby -v            (from the repo root)

Expected values come from the client (SLPM_624.64), not from the code under test.
Every frame the gate sends is checked with the client's own receive rules
(serial + MD5, test_wire.ClientModel).
"""
import json
import os
import socket
import struct
import tempfile
import threading
import unittest

from . import bf, gate, model, relay, wire
from .test_wire import ClientModel

LOGS = os.path.join(os.path.dirname(__file__), "..", "data", "test-logs")


class CipherTest(unittest.TestCase):
    def test_known_vectors(self):
        c = bf._cipher()
        self.assertEqual(c.encrypt(b"\0" * 8).hex(), "13caa3a68c4fe6bf")      # enciphered padding
        self.assertEqual(c.encrypt(bytes.fromhex("0161616100000000")).hex(),  # example 0x4007, id "aaa"
                         "7de22730c2c1f962")
        self.assertEqual(bf.encrypt_payload(b"abc123\0pass99\0").hex(),         # issued-account example
                         "ee11e6a101a3ca7b018d3dba08d6025c")

    def test_example_create_char(self):
        # name "TEST", postal 1000001, birthday 1000101 (CYYMMDD: 2000-01-01), B 0, D 0, E 1
        ct = bytes.fromhex("7b9c92ce3a640d5413caa3a68c4fe6bf13caa3a68c4fe6bf"
                           "13caa3a68c4fe6bfc2a5a55455dadf82dcf283aa641c0abc")
        name, flags, extra = model.parse_create_char(ct)
        # flags = (rank 0, prefecture B=0, class 1); postal/birthday/job/sex kept as profile
        self.assertEqual((name, flags, extra),
                         ("TEST", (0, 0, 1), {"A": 1000001, "C": 1000101, "D": 0, "E": 1}))

    def test_roundtrip(self):
        data = os.urandom(40)
        self.assertEqual(bf.decrypt_payload(bf.encrypt_payload(data)), data)


class RecordTest(unittest.TestCase):
    def test_user_record(self):
        ch = model.Character(7, "あいう", stats=[1, 2, 3, 4, 5, 6], flags=(9, 8, 7))
        r = model.user_record(7, ch, room_id=3)
        self.assertEqual(len(r), 71)
        self.assertEqual(r[:6], "あいう".encode("cp932"))
        fields = struct.unpack(">9I", r[32:68])
        # uid; s2; s3; s4; s1; status; s6; s5; location
        self.assertEqual(fields, (7, 2, 3, 4, 1, model.STATUS_ROOM, 6, 5, 3))
        self.assertEqual(tuple(r[68:71]), (9, 8, 7))
        lobby = model.user_record(7, ch)
        self.assertEqual(struct.unpack(">2I", lobby[52:56] + lobby[64:68]),
                         (model.STATUS_LOBBY, model.NO_ROOM))

    def test_add_and_stats_records(self):
        ch = model.Character(7, "A", stats=[1, 2, 3, 4, 5, 6])
        self.assertEqual(len(model.user_add_record(7, ch)), 63)
        self.assertEqual(len(model.stats_record(7, ch)), 31)

    def test_room_records(self):
        r = model.Room(4, b"ROOM", b"pw", 1, 2, owner=11)
        r.members = [11, 22]
        rec = r.record()
        # id; sortkey; name\0; M; uids; owner; state; A; B; has_pw
        self.assertEqual(rec, struct.pack(">II", 4, 4) + b"ROOM\0" +
                         struct.pack(">IIIII", 2, 11, 22, 11, 0) + bytes([1, 2, 1]))
        up = r.upsert()
        # id; sortkey; state; name\0; M; uids; owner; has_pw; A; B
        self.assertEqual(up, struct.pack(">III", 4, 4, 0) + b"ROOM\0" +
                         struct.pack(">IIII", 2, 11, 22, 11) + bytes([1, 1, 2]))
        self.assertEqual(r.delete(), struct.pack(">III", 4, 4, model.ROOM_DELETE))

    def test_parse_create_room(self):
        open_ = b"\0" + b"ROOM".ljust(32, b"\0") + bytes([1, 3])
        self.assertEqual(model.parse_create_room(open_), (b"ROOM", None, 1, 3))
        locked = b"\1" + b"R2".ljust(32, b"\0") + b"secret".ljust(32, b"\0") + bytes([2, 4])
        self.assertEqual(model.parse_create_room(locked), (b"R2", b"secret", 2, 4))


class RankTest(unittest.TestCase):
    def test_client_formula_spot_values(self):
        """Values worked from the client's rank tables."""
        self.assertEqual(model.rank(5, 5, 0), 0)            # < 10 plays: always 8th kyu
        self.assertEqual(model.rank(10, 10, 0), 1)
        self.assertEqual(model.rank(50, 40, 50000), 10)     # 3rd dan
        self.assertEqual(model.rank(100, 80, 90000), 15)    # 8th dan
        self.assertEqual(model.rank(200, 200, 90000), 17)   # God

    def test_points(self):
        self.assertEqual(model.points_delta(2, 5, "win"), 39)
        self.assertEqual(model.points_delta(5, 2, "win"), 30)
        self.assertEqual(model.points_delta(0, 0, "draw"), 15)
        self.assertEqual(model.points_delta(0, 9, "loss"), 10)


class MigrationTest(unittest.TestCase):
    def test_old_accounts_file(self):
        """Old files: per-account char ids (both 1) and enciphered POPN<n> names."""
        enc_name = bytes.fromhex("7b9c92ce3a640d5413caa3a68c4fe6bf13caa3a68c4fe6bf"
                                 "13caa3a68c4fe6bf")
        old = {"accounts": {
            "aa11": {"key": "aa11", "next_char": 2, "characters": [
                {"id": 1, "name": "POPN1", "raw_name": enc_name.hex(), "stats": [0] * 6,
                 "flags": [255, 0, 1]}]},
            "bb22": {"key": "bb22", "next_char": 2, "characters": [
                {"id": 1, "name": "ZED", "raw_name": "", "stats": [0] * 6, "flags": [255, 0, 1]}]}},
            "rooms": {"1": {"id": 1, "name": "54455354", "password": None, "mode": 0,
                            "host_key": "x", "members": ["x"]}},
            "next_room": 2}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "accounts.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(old, f)
            st = model.ServerState(path)
            ids = sorted(c.id for a in st.accounts.values() for c in a.characters)
            self.assertEqual(ids, [1, 2])
            self.assertEqual(st.accounts["aa11"].characters[0].name, "TEST")
            # old (B, D, E) flags -> (rank, prefecture, class); 255 is not a prefecture
            self.assertEqual(st.accounts["aa11"].characters[0].flags, [0, 0, 1])
            self.assertEqual(st.rooms, {})                    # rooms are never persisted
            c = st.add_character("bb22", "NEW")
            self.assertEqual(c.id, 3)                         # global counter
            self.assertTrue(st.adopt_legacy("aa11", "aaa"))
            self.assertIn("aaa", st.accounts)


class Player:
    """A fake console on a socketpair, talking to a real gate.Session."""

    def __init__(self, ip):
        os.makedirs(LOGS, exist_ok=True)
        self.sock, srv = socket.socketpair()
        self.sock.settimeout(5)
        self.session = gate.Session(srv, (ip, 40000), LOGS)
        self.thread = threading.Thread(target=self.session.run, daemon=True)
        self.thread.start()
        self.client = ClientModel(wire.KEY)
        self.serial = 0
        self.buf = b""
        self.inbox = []
        self.uid = None

    def send(self, op, payload=b""):
        self.serial += 1
        self.sock.sendall(wire.build(op, self.serial, payload))

    def _pump(self):
        self.buf += self.sock.recv(65536)
        while len(self.buf) >= wire.HDR:
            length = struct.unpack(">H", wire.xor(self.buf[:4])[2:4])[0]
            if len(self.buf) < wire.HDR + length:
                break
            raw, self.buf = self.buf[:wire.HDR + length], self.buf[wire.HDR + length:]
            got = self.client.recv(raw)
            assert isinstance(got, tuple), f"client rejected a frame: {got}"
            self.inbox.append(got)

    def expect(self, op):
        while True:
            for i, (o, p) in enumerate(self.inbox):
                if o == op:
                    del self.inbox[i]
                    return p
            self._pump()

    def status(self, op):
        return struct.unpack(">i", self.expect(op)[:4])[0]

    def login_and_enter(self, login_id, char_name):
        self.send(0x4003)
        chal = self.expect(0x4004)
        body = (bytes([1]) + login_id.encode().ljust(0x100, b"\0")
                + gate.login_digest(login_id, chal, "pw"))
        self.send(0x4007, bf.encrypt_payload(body))
        assert self.status(0x4008) == 0
        create = (char_name.encode("cp932").ljust(32, b"\0") + struct.pack(">II", 1000001, 1000101)
                  + bytes([3, 4, 1]))
        self.send(0x4100, bf.encrypt_payload(create))
        assert self.status(0x4101) == 0
        self.send(0x4104)
        chars = self.expect(0x4105)
        self.uid = struct.unpack(">I", chars[:4])[0]
        assert chars[4:4 + len(char_name)] == char_name.encode("cp932")
        self.send(0x4106, struct.pack(">I", self.uid))
        assert self.status(0x4107) == 0
        self.send(0x4202, struct.pack(">I", 0))
        assert self.status(0x4203) == 0

    def user_list(self):
        self.send(0x4402)
        n = struct.unpack(">I", self.expect(0x4403))[0]
        recs = b""
        while len(recs) < n * 71:
            recs += self.expect(0x4404)
        self.expect(0x4405)
        return self.parse_users(n, recs)

    @staticmethod
    def parse_users(n, recs):
        out = {}
        for i in range(n):
            r = recs[i * 71:(i + 1) * 71]
            f = struct.unpack(">9I", r[32:68])   # uid s2 s3 s4 s1 status s6 s5 loc
            out[f[0]] = {"name": r[:32].split(b"\0")[0], "status": f[5], "loc": f[8],
                         "wins": f[1], "losses": f[2], "plays": f[4], "hiscore": f[6],
                         "points": f[7], "rank": r[68], "pref": r[69]}
        return out


class TwoPlayerTest(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("POPN_AUTH_DB", "POPN_AUTH_OPEN", "POPN_FAKE_CHAR")}
        os.environ["POPN_AUTH_OPEN"] = "1"
        os.environ.pop("POPN_AUTH_DB", None)
        os.environ.pop("POPN_FAKE_CHAR", None)
        model.init(None)
        gate.LOBBY.clear()

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_full_match_flow(self):
        a, b = Player("10.0.0.1"), Player("10.0.0.2")
        a.login_and_enter("alice", "ALICE")
        b.login_and_enter("bob", "BOB")
        self.assertNotEqual(a.uid, b.uid)                     # global char ids
        self.assertEqual(struct.unpack(">I", a.expect(0x4400)[32:36])[0], b.uid)

        users = a.user_list()
        self.assertEqual(set(users), {a.uid, b.uid})
        self.assertEqual(users[a.uid]["loc"], model.NO_ROOM)
        self.assertEqual(users[a.uid]["status"], model.STATUS_LOBBY)

        # A creates a room: 0x4301 + 0x430a, then presence pushes.
        a.send(0x4300, b"\0" + b"ROOM".ljust(32, b"\0") + bytes([1, 2]))
        rid = a.status(0x4301)
        self.assertEqual(a.status(0x430A), rid)
        self.assertEqual(a.expect(0x4406), struct.pack(">II", a.uid, rid))   # the mover too
        self.assertEqual(b.expect(0x4406), struct.pack(">II", a.uid, rid))

        # A's own record now says "in room rid".
        users = a.user_list()
        self.assertEqual((users[a.uid]["status"], users[a.uid]["loc"]), (model.STATUS_ROOM, rid))
        a.send(0x4302)
        a.expect(0x4303)
        room = a.expect(0x4304)
        self.assertEqual(struct.unpack(">I", room[:4])[0], rid)
        self.assertEqual(a.status(0x4305), 0)

        # B joins: A (the host) hears 0x4406, 0x4306 (2 members, owner A) and 0x4512.
        b.send(0x4309, struct.pack(">I", rid) + b"\0")
        self.assertEqual(b.status(0x430A), rid)
        self.assertEqual(a.expect(0x4406), struct.pack(">II", b.uid, rid))
        up = a.expect(0x4306)
        while struct.unpack(">I", up[up.index(b"\0", 12) + 1:][:4])[0] != 2:
            up = a.expect(0x4306)                             # skip the create-time upsert
        tail = up[up.index(b"\0", 12) + 1:]
        self.assertEqual(struct.unpack(">4I", tail[:16]), (2, a.uid, b.uid, a.uid))
        self.assertEqual(a.status(0x4512), 0)

        # A full room refuses a third player.
        c = Player("10.0.0.3")
        c.login_and_enter("carol", "CAROL")
        c.send(0x4309, struct.pack(">I", rid) + b"\0")
        self.assertEqual(c.status(0x430A), gate.ERR_ROOM_FULL)

        # START pairing: first press waits (0x4503), peer gets 0x4510; second press goes.
        a.send(0x4500)
        self.assertEqual(a.status(0x4503), 0)
        self.assertEqual(b.status(0x4510), 0)
        b.send(0x4500)
        self.assertEqual(b.status(0x4506), 0)
        self.assertEqual(a.status(0x4510), 0)

        # Rendezvous: each gets the other's address as the gate sees it.
        a.send(0x4409, struct.pack(">I", b.uid))
        self.assertEqual(a.expect(0x440B), struct.pack(">I", b.uid) + b"10.0.0.2\0")
        b.send(0x4409, struct.pack(">I", a.uid))
        self.assertEqual(b.expect(0x440B), struct.pack(">I", a.uid) + b"10.0.0.1\0")

        # P2P failure round: first report echoed + forwarded, the peer's reaction absorbed.
        b.send(0x451F, struct.pack(">I", a.uid))
        self.assertEqual(b.status(0x451F), 0)
        self.assertEqual(a.status(0x451F), 0)

        # Battle start and result go to both.
        a.send(0x4517)
        self.assertEqual(a.status(0x4518), 0)
        self.assertEqual(b.status(0x4518), 0)
        a.send(0x4519, bytes([0x20]) + struct.pack(">I", a.uid))
        self.assertEqual(a.status(0x451A), 0)
        self.assertEqual(b.status(0x451A), 0)

        # Stats: B reports first and asks for the user list, which is HELD until A's
        # 0x4508 arrives, so B's result screen sees both players' new numbers.
        b.send(0x4508, bytes([8, 7, 0, 40]) + struct.pack(">IIII", 1234, 9, 55, 3)
               + b"\0" * 104)
        self.assertEqual(b.status(0x450A), 0)
        b.send(0x4402)
        a.send(0x4508, bytes([4, 12, 0, 90]) + struct.pack(">IIII", 54321, 20, 140, 6)
               + b"\0" * 104)
        self.assertEqual(a.status(0x450A), 0)
        n = struct.unpack(">I", b.expect(0x4403))[0]
        recs = b""
        while len(recs) < n * 71:
            recs += b.expect(0x4404)
        b.expect(0x4405)
        users = Player.parse_users(n, recs)
        self.assertEqual((users[a.uid]["plays"], users[a.uid]["wins"], users[a.uid]["points"],
                          users[a.uid]["hiscore"]), (1, 1, 30, 54321))
        self.assertEqual((users[b.uid]["losses"], users[b.uid]["points"]), (1, 10))
        self.assertEqual(users[a.uid]["pref"], 3)                 # prefecture B from 0x4100
        # The 0x4508 totals behind the chain/dtama/chara/attack rankings.
        ca, cb = model.STATE.character(a.uid), model.STATE.character(b.uid)
        self.assertEqual((ca.battle["games"], ca.battle["max_chain"], ca.battle["balls_total"],
                          ca.battle["atk_total"], ca.battle["drop_total"]), (1, 6, 140, 90, 20))
        self.assertEqual(cb.battle["max_chain"], 3)
        self.assertEqual(model.STATE.chara["12"], {"games": 1, "wins": 1, "atk_total": 90,
                                                   "drop_total": 20})
        self.assertEqual(model.STATE.chara["7"]["wins"], 0)
        # The third player in the lobby hears both new stat lines.
        got = {struct.unpack(">I", c.expect(0x450F)[:4])[0] for _ in range(2)}
        self.assertEqual(got, {a.uid, b.uid})

        # Chat: echoed to the sender and delivered to the room, kind 1, NUL-terminated.
        a.send(0x4408, b"\0" + b"\xff" * 4 + b"hi")
        want = bytes([1]) + struct.pack(">I", a.uid) + b"hi\0"
        self.assertEqual(a.expect(0x4408), want)
        self.assertEqual(b.expect(0x4408), want)

        # A leaves: B gets "left" first, then the room with B as the new owner.
        a.send(0x430B)
        self.assertEqual(a.status(0x430C), 0)
        self.assertEqual(b.status(0x4513), 0)
        want = (1, b.uid, b.uid)                              # M=1, [B], owner B
        seen = []
        while not seen or seen[-1] != want:                   # older upserts may queue first
            up = b.expect(0x4306)
            seen.append(struct.unpack(">3I", up[up.index(b"\0", 12) + 1:][:12]))
            self.assertLess(len(seen), 5, seen)
        self.assertEqual(b.expect(0x4407), struct.pack(">I", a.uid))

        # B hangs up: A's roster drops B.
        b.send(0x0003)
        self.assertEqual(a.expect(0x4401), struct.pack(">I", b.uid))
        for p in (a, b, c):
            p.sock.close()

    def test_same_character_twice(self):
        """A live duplicate gets 'already logged in' (-242); a stale one is taken over."""
        a = Player("10.0.0.1")
        a.login_and_enter("alice", "ALICE")
        b = Player("10.0.0.2")
        b.send(0x4106, struct.pack(">I", a.uid))
        self.assertEqual(b.status(0x4107), 0)
        b.send(0x4202, struct.pack(">I", 0))
        self.assertEqual(b.status(0x4203), gate.ERR_LOGGED_IN)
        self.assertIs(gate.LOBBY[a.uid], a.session)
        a.session.last_rx -= gate.STALE_AFTER + 1          # A's console went silent
        b.send(0x4202, struct.pack(">I", 0))
        self.assertEqual(b.status(0x4203), 0)
        self.assertIs(gate.LOBBY[a.uid], b.session)
        for p in (a, b):
            p.sock.close()

    def test_issue_account_then_login(self):
        p = Player("10.0.0.9")
        p.send(0x4005)
        plain = bf.decrypt_payload(p.expect(0x4006))
        login_id, pw = [s.decode() for s in plain.split(b"\0")[:2]]
        self.assertTrue(0 < len(login_id) <= 12 and 0 < len(pw) <= 12)
        self.assertFalse(set(login_id + pw) & set("o0I1lL"))
        # The issued password is enforced even in open mode.
        p.send(0x4003)
        chal = p.expect(0x4004)
        bad = bytes([1]) + login_id.encode().ljust(0x100, b"\0") + gate.login_digest(login_id, chal, "nope")
        p.send(0x4007, bf.encrypt_payload(bad))
        self.assertEqual(p.status(0x4008), gate.ERR_LOGIN)
        p.send(0x4003)
        chal = p.expect(0x4004)
        good = bytes([1]) + login_id.encode().ljust(0x100, b"\0") + gate.login_digest(login_id, chal, pw)
        p.send(0x4007, bf.encrypt_payload(good))
        self.assertEqual(p.status(0x4008), 0)
        p.sock.close()


class RelayTest(unittest.TestCase):
    """Two relay-patched consoles both CONNECT to the relay; it pairs and splices them."""

    def setUp(self):
        self.relay = relay.Relay(log=lambda *a: None)
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.relay.serve_on, args=(self.srv,), daemon=True).start()

    def tearDown(self):
        self.srv.close()

    def dial(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return s

    def recv_exact(self, s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def test_pairs_and_relays_both_ways(self):
        self.relay.expect(("room", 1), "127.0.0.1")   # both players share an IP here
        a = self.dial()
        a.sendall(b"A" * 0x1E8)                       # lockstep: first frame before partner exists
        b = self.dial()
        b.sendall(b"B" * 0x1E8)
        self.assertEqual(self.recv_exact(b, 0x1E8), b"A" * 0x1E8)
        self.assertEqual(self.recv_exact(a, 0x1E8), b"B" * 0x1E8)
        a.close()
        self.assertEqual(b.recv(10), b"")             # one side leaving ends the pair
        b.close()

    def test_unregistered_ip_is_closed(self):
        s = self.dial()
        self.assertEqual(s.recv(10), b"")
        s.close()

    def test_gate_hands_out_relay_address(self):
        model.init(None)
        gate.LOBBY.clear()
        old = gate.RELAY, gate.RELAY_HOST
        gate.RELAY, gate.RELAY_HOST = self.relay, "203.0.113.7"
        try:
            p = Player("198.51.100.4")
            p.session.uid = 5
            p.send(0x4409, struct.pack(">I", 6))
            self.assertEqual(p.expect(0x440B), struct.pack(">I", 6) + b"203.0.113.7\0")
            self.assertEqual(self.relay._key_for("198.51.100.4"), ("pair", (5, 6)))
            p.sock.close()
        finally:
            gate.RELAY, gate.RELAY_HOST = old


if __name__ == "__main__":
    unittest.main()
