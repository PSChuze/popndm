"""adminport.py: the gate in an OpenLobby admin panel (the protocol in olport.py).

    python -m unittest popn.test_adminport -v            (from the repo root)

Two fake consoles (test_lobby.Player, real gate.Sessions) log in and one opens a room; the admin
port is driven over HTTP the way the panel does: no token, the description, the status with both
players in it, the player lookup by linked login, and Disconnect, which must close that player's connection and reach the other one
as the same 0x4401 "user left" the gate sends when a console goes away. The Rankings page is
read the same way, with characters whose stats are set directly.
"""
import json
import os
import struct
import unittest
import urllib.error
import urllib.parse
import urllib.request

from . import adminport, gate, model, web
from .test_lobby import Player

TOKEN = "selftest-token-0123"


class AdminPortTest(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("POPN_AUTH_DB", "POPN_AUTH_OPEN", "POPN_FAKE_CHAR")}
        os.environ["POPN_AUTH_OPEN"] = "1"
        os.environ.pop("POPN_AUTH_DB", None)
        os.environ.pop("POPN_FAKE_CHAR", None)
        model.init(None)
        gate.LOBBY.clear()
        self.saved = adminport.TOKEN, adminport.BIND, adminport.PORT, adminport.UPDATE_REPO
        # no update notice here: it would ask GitHub (test_updates.py covers it)
        adminport.TOKEN, adminport.BIND, adminport.PORT, adminport.UPDATE_REPO = TOKEN, "127.0.0.1", 0, ""
        self.port = adminport.build(gate.LOBBY, log=lambda m: None)
        host, n = self.port.start()
        self.url = "http://127.0.0.1:%d" % n

    def tearDown(self):
        self.port.stop()
        adminport.TOKEN, adminport.BIND, adminport.PORT, adminport.UPDATE_REPO = self.saved
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def page(self, path, method="GET"):
        req = urllib.request.Request(self.url + path, data=b"" if method == "POST" else None,
                                     headers={"X-Devtool-Token": TOKEN}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode("utf-8")

    def call(self, path, body=None, token=TOKEN):
        req = urllib.request.Request(self.url + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Devtool-Token": token, "X-Devtool-Actor": "admin"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_refuses_without_token(self):
        self.assertEqual(self.call("/openlobby/module.json", token="nope")[0], 403)
        self.assertEqual(self.call("/openlobby/status.json", token="")[0], 403)
        self.assertEqual(self.call("/openlobby/actions/kick", {"name": "x"}, token="")[0], 403)

    def test_wide_bind_needs_a_token(self):
        adminport.TOKEN, adminport.BIND = "", "172.17.0.1"
        with self.assertRaises(ValueError):
            adminport.build(gate.LOBBY)
        self.assertIsNone(adminport.start(gate.LOBBY, log=lambda m: None))   # off: PORT is 0

    def test_describes_itself(self):
        st, desc = self.call("/openlobby/module.json")
        self.assertEqual(st, 200)
        self.assertEqual((desc["key"], desc["title"], desc["short"], desc["platform"]),
                         ("popn", "pop'n Puzzle-dama Online", "pop'n", "PS2 HDD"))
        self.assertEqual([(a["id"], a["kind"]) for a in desc["actions"]], [("kick", "kick")])
        self.assertEqual(desc["services"][0]["containers"], ["popn-gate-1", "popn-web-1"])
        self.assertEqual([(t["id"], t["path"]) for t in desc["tools"]], [("rankings", "rankings")])
        self.assertTrue(desc["player"])       # by the logins the website linked, never by name
        self.assertTrue(desc["players"])      # the picker
        self.assertEqual(desc["actions"][0]["fields"][0]["type"], "player")

    def test_player_picker(self):
        a = Player("10.0.0.1")
        a.login_and_enter("alice01", "ALICE")
        model.STATE.add_character("alice01", "ALICE2")
        model.STATE.add_character("bob02", "BOB")
        st, got = self.call("/openlobby/players.json?q=ali&limit=5")
        self.assertEqual(st, 200)
        self.assertEqual([(p["id"], p["online"]) for p in got["players"]],
                         [("ALICE", True), ("ALICE2", False)])
        self.assertEqual(got["players"][0]["detail"], "login alice01, rank 0")
        st, got = self.call("/openlobby/players.json?q=BOB02")      # by login id, any case
        self.assertEqual([p["label"] for p in got["players"]], ["BOB"])
        st, got = self.call("/openlobby/players.json?q=")            # online now
        self.assertEqual([p["id"] for p in got["players"]], ["ALICE"])
        self.assertEqual(self.call("/openlobby/players.json?q=a&limit=1")[1]["players"][0]["id"], "ALICE")
        self.assertEqual(self.call("/openlobby/players.json?limit=99")[0], 400)
        a.sock.close()

    def lookup(self, links, handles="ALICE"):
        q = urllib.parse.urlencode({"polid": "AB12345", "handles": handles, "members": "7",
                                    "links": json.dumps(links)})
        return self.call("/openlobby/player.json?" + q)

    def test_player_lookup(self):
        a = Player("10.0.0.1")
        a.login_and_enter("alice01", "ALICE")      # its 0x4100 profile: postal 1000001, born 1000101
        acct = model.STATE.accounts["alice01"]
        acct.password = "s3cretpw"
        c = acct.characters[0]
        c.stats, c.flags = [20, 15, 5, 0, 500, 61000], [9, 12, 1]
        model.STATE.add_character("alice01", "ALICE2")
        model.STATE.add_character("bob02", "BOB")

        st, got = self.lookup({"popn": [{"key": "alice01", "name": "alice01"}],
                               "other": [{"key": "bob02", "name": "bob02"}]})
        self.assertEqual(st, 200)
        self.assertEqual(got, {"characters": [
            {"name": "ALICE", "detail": "login alice01, rank 9, Tokyo", "online": True,
             "facts": [{"label": "matches", "value": 20}, {"label": "won / lost", "value": "15 / 5"},
                       {"label": "points", "value": 500}, {"label": "high score", "value": 61000}]},
            {"name": "ALICE2", "detail": "login alice01, rank 0, %s" % web.pref_name(0, "e"),
             "online": False,
             "facts": [{"label": "matches", "value": 0}, {"label": "won / lost", "value": "0 / 0"},
                       {"label": "points", "value": 0}, {"label": "high score", "value": 0}]}]})
        text = json.dumps(got)
        for secret in ("s3cretpw", "10.0.0.1", "BOB", "1000001", "1000101", "password"):
            self.assertNotIn(secret, text)

        # a login the gate never saw, or no pop'n link at all: nothing (the port sends {}),
        # and the lookup must not create an account for the unknown key
        self.assertEqual(self.lookup({"popn": [{"key": "ghost99", "name": "ghost99"}]}), (200, {}))
        self.assertNotIn("ghost99", model.STATE.accounts)
        self.assertEqual(self.lookup({}, handles="ALICE,BOB"), (200, {}))
        self.assertEqual(self.lookup({"other": [{"key": "alice01", "name": "alice01"}]}), (200, {}))
        st, got = self.lookup({"popn": [{"key": "bob02", "name": "bob02"}]}, handles="")
        self.assertEqual([c["name"] for c in got["characters"]], ["BOB"])
        a.sock.close()

    def test_rankings_page(self):
        st, ctype, body = self.page("/rankings")
        self.assertEqual((st, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn("No one has played yet.", body)

        # stats: plays, wins, losses, draws, points, hiscore; flags: rank, prefecture, class
        model.STATE.add_character("alice01", "ALICE", stats=[20, 15, 5, 0, 500, 61000], flags=[9, 12, 1])
        model.STATE.add_character("bob02", "<b>BOB</b>", stats=[12, 4, 8, 0, 200, 0], flags=[3, 6, 1])
        model.STATE.add_character("carol03", "CAROL", flags=[0, 0, 1])       # never played
        a = Player("10.0.0.1")
        a.login_and_enter("alice01", "ALICE")

        st, ctype, body = self.page("/rankings")
        self.assertEqual(st, 200)
        self.assertNotIn("<script", body)
        self.assertIn("&lt;b&gt;BOB&lt;/b&gt;", body)           # names are escaped
        self.assertNotIn("<b>BOB", body)
        self.assertNotIn("CAROL", body)
        self.assertIn('<span class="mono">alice01</span>', body)
        self.assertEqual(body.count('<span class="pill">online</span>'), 7)   # ALICE: all but Max chain
        self.assertIn("<td>75.00%</td>", body)                    # win rate, 15 of 20
        self.assertIn("<td>Tokyo</td><td>500</td><td>1</td>", body)   # prefecture: points, players
        for title in ("Rank", "Points", "Wins", "Losses", "Win rate", "Matches played",
                      "Max chain", "High score", "Prefectures"):
            self.assertIn("<h2>%s</h2>" % title, body)
        self.assertNotIn("Balls", body)                           # served empty: not shown
        # the high score table leaves out a character with none, as the game's does
        score = body.split("<h2>High score</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("ALICE", score)
        self.assertNotIn("BOB", score)

        self.assertEqual(self.page("/rankings", "POST")[0], 405)
        a.sock.close()

    def test_status_and_disconnect(self):
        a, b = Player("10.0.0.1"), Player("10.0.0.2")
        a.login_and_enter("alice01", "ALICE")
        b.login_and_enter("bob02", "BOB")
        a.send(0x4300, b"\0" + b"Friday".ljust(32, b"\0") + bytes([1, 2]))
        rid = a.status(0x4301)
        a.status(0x430A)

        st, live = self.call("/openlobby/status.json")
        self.assertEqual(st, 200)
        self.assertEqual(live["online"], 2)
        self.assertEqual(live["players"], [{"name": "ALICE", "where": "Friday"},
                                           {"name": "BOB", "where": "lobby"}])
        self.assertEqual(live["rooms"], [{"name": "Friday", "players": 1, "max": 2, "state": "open"}])
        self.assertEqual(live["summary"], "")
        self.assertNotIn("update", live)

        self.assertEqual(self.call("/openlobby/actions/kick", {"name": "Carol"})[1],
                         {"ok": False, "msg": "Not online."})
        self.assertEqual(self.call("/openlobby/actions/kick", {"name": " "})[1],
                         {"ok": False, "msg": "No player named."})

        # an account's Kick: {"polid", "handles"}; the handle matches the character in any case
        st, out = self.call("/openlobby/actions/kick", {"polid": "TEST0001", "handles": "Frank,alice"})
        self.assertEqual(out, {"ok": True, "msg": "Disconnected ALICE."})
        a.sock.settimeout(5)
        while a.sock.recv(65536):          # the gate closes A's connection (end of stream)
            pass
        a.sock.close()                     # and A's end goes too; on Windows the gate's recv waits for it
        self.assertEqual(struct.unpack(">I", b.expect(0x4401))[0], a.uid)    # B hears A left
        self.assertNotIn(rid, model.STATE.rooms)                             # and A's room is gone

        st, live = self.call("/openlobby/status.json")
        self.assertEqual((live["online"], live["players"], live["rooms"]),
                         (1, [{"name": "BOB", "where": "lobby"}], []))

        # the panel's own form: a login id works too
        st, out = self.call("/openlobby/actions/kick", {"name": "BOB02"})
        self.assertEqual(out, {"ok": True, "msg": "Disconnected BOB."})
        b.sock.close()


if __name__ == "__main__":
    unittest.main()
