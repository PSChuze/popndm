"""live.py: the boards' snapshot of lobby presence, rooms and recent matches."""
import json
import os
import tempfile
import unittest

from . import gate, live, model


class _S:
    def __init__(self, room=None):
        self.room = room


class LiveTest(unittest.TestCase):
    def setUp(self):
        model.STATE = model.ServerState(None)
        live._matches.clear()
        self.a = model.STATE.add_character("alice", "ALICE")
        self.b = model.STATE.add_character("bob", "BOB")

    def test_empty(self):
        snap = live.snapshot({}, now=100)
        self.assertEqual(snap["online"], 0)
        self.assertEqual(snap["updated"], 100)
        self.assertEqual((snap["players"], snap["rooms"], snap["matches"]), ([], [], []))

    def test_players_and_rooms(self):
        r = model.STATE.create_room("ルーム".encode("cp932"), b"pw", 0, 0, owner=self.a.id)
        lobby = {self.a.id: _S(r), self.b.id: _S()}
        snap = live.snapshot(lobby)
        self.assertEqual(snap["online"], 2)
        self.assertEqual([p["name"] for p in snap["players"]], ["ALICE", "BOB"])
        self.assertEqual(snap["players"][0]["room"], r.id)
        self.assertIsNone(snap["players"][1]["room"])
        self.assertEqual(snap["rooms"], [{"id": r.id, "name": "ルーム", "players": ["ALICE"],
                                          "locked": True, "in_battle": False}])

    def test_empty_seed_room_hidden(self):
        model.STATE.create_room(b"TEST", None, 0, 2, owner=0, seed=True)
        self.assertEqual(live.snapshot({})["rooms"], [])

    def test_match_recorded_winner_first(self):
        with model.STATE.lock:
            gate.apply_match_result([self.b.id, self.a.id], self.a.id)
        m = live.snapshot({})["matches"]
        self.assertEqual(len(m), 1)
        self.assertEqual([(p["name"], p["outcome"]) for p in m[0]["players"]],
                         [("ALICE", "win"), ("BOB", "loss")])

    def test_matches_capped(self):
        with model.STATE.lock:
            for _ in range(live.MATCHES_KEPT + 5):
                gate.apply_match_result([self.a.id, self.b.id], 0)
        m = live.snapshot({})["matches"]
        self.assertEqual(len(m), live.MATCHES_KEPT)
        self.assertEqual(m[0]["n"], m[1]["n"] + 1)

    def test_write_is_json(self):
        with tempfile.TemporaryDirectory() as d:
            dest = live.path(os.path.join(d, "accounts.json"))
            self.assertEqual(dest, os.path.join(d, "board", "live.json"))
            live.write(dest, live.snapshot({}))           # creates board/
            with open(dest, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["game"], "popn")

    def test_no_secrets(self):
        model.STATE.issue_account("carol", "hunter2")
        text = json.dumps(live.snapshot({}))
        self.assertNotIn("hunter2", text)
        self.assertNotIn("carol", text)

    def test_standings(self):
        with model.STATE.lock:
            for i in range(12):
                gate.apply_match_result([self.a.id, self.b.id], self.a.id if i % 3 else self.b.id)
        self.a.battle["max_chain"] = 5
        snap = live.snapshot({})
        st = snap["standings"]
        self.assertEqual(set(st), set(live.STANDINGS))
        self.assertEqual([r["name"] for r in st["rank"]], ["ALICE", "BOB"])
        self.assertEqual(st["rate"][0]["value"], 66.7)            # 8 of 12
        self.assertEqual([r["name"] for r in st["chain"]], ["ALICE"])   # BOB's 0 is left out
        self.assertEqual(snap["totals"], {"characters": 2, "matches": 12})
        self.assertEqual(snap["names"][str(self.a.id)], "ALICE")

    def test_rate_needs_ten_matches(self):
        with model.STATE.lock:
            for _ in range(9):
                gate.apply_match_result([self.a.id, self.b.id], self.a.id)
        self.assertEqual(live.snapshot({})["standings"]["rate"], [])

    def test_ties_share_a_place(self):
        with model.STATE.lock:
            gate.apply_match_result([self.a.id, self.b.id], 0)
        rows = live.snapshot({})["standings"]["points"]
        self.assertEqual([r["place"] for r in rows], [1, 1])


if __name__ == "__main__":
    unittest.main()
