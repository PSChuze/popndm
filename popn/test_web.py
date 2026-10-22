"""Self-test for the ranking server (web.py).

    python -m unittest popn.test_web -v            (from the repo root)

ClientRanking is written from the client's parser, not from web.py: count
the '\\n', parse n-1 records with strtok on TAB/LF.
"""
import http.client
import threading
import unittest

from . import model, web
from http.server import ThreadingHTTPServer


def client_parse(body, cat):
    """The client's view of a ranking body: a list of field lists."""
    n = body.count(b"\n")
    if n < 1:
        raise AssertionError("body has no newline: the client mallocs (n-1)*0x38 < 0")
    toks = [t for t in body.replace(b"\t", b"\n").split(b"\n") if t]  # strtok merges runs
    width = {"division": 4, "chara": 2, "attack": 2}.get(cat, 6)
    recs = [toks[i:i + width] for i in range(0, (n - 1) * width, width)]
    for r in recs:
        if len(r) != width:
            raise AssertionError("short record (atoi(NULL) on the client): %r" % r)
    return recs


def char(cid, name, plays, wins, losses, points, hiscore, pref):
    c = model.Character(cid, name, stats=[plays, wins, losses, 0, points, hiscore],
                        flags=[0, pref, 1])
    c.recompute_rank()
    return c


CHARS = [
    char(1, "POPN1", 20, 15, 5, 500, 61000, 12),
    char(2, "ぽっぷ", 12, 4, 8, 200, 25000, 6),
    char(3, "Bob", 0, 0, 0, 0, 0, 0),                # never played: not listed
    char(4, "Tied", 20, 15, 5, 500, 30000, 12),
]


class RankingBody(unittest.TestCase):
    def test_every_category_parses(self):
        for cat in web.RANKINGS:
            for lang in "je":
                recs = client_parse(web.ranking_body(cat, lang, CHARS), cat)
                for r in recs:
                    self.assertLessEqual(len(r[2 if len(r) == 6 else 1]), web.NAME_BYTES)

    def test_empty_state_is_one_newline(self):
        for cat in web.RANKINGS:
            self.assertEqual(web.ranking_body(cat, "j", []), b"\n")

    def test_points_order_ties_and_fields(self):
        recs = client_parse(web.ranking_body("point", "j", CHARS), "point")
        self.assertEqual([r[1] for r in recs], [b"1", b"4", b"2"])
        self.assertEqual([r[0] for r in recs], [b"1", b"1", b"3"])   # tie shares a place
        self.assertEqual(recs[0][3], b"500")
        self.assertEqual(int(recs[0][4]), CHARS[0].flags[model.RANK])
        self.assertEqual(recs[0][5], "東京都".encode("euc_jp"))
        self.assertEqual(recs[2][2], "ぽっぷ".encode("euc_jp"))

    def test_english_prefectures_fit(self):
        for p in web.PREFS_EN:
            self.assertLessEqual(len(p), web.PREF_BYTES)
        recs = client_parse(web.ranking_body("kati", "e", CHARS), "kati")
        self.assertEqual(recs[0][5], b"Tokyo")

    def test_win_rate_in_ten_thousandths(self):
        recs = client_parse(web.ranking_body("pwin", "j", CHARS), "pwin")
        self.assertEqual(recs[0][3], b"7500")

    def test_max_chain(self):
        chars = [char(1, "A", 5, 3, 2, 0, 0, 0), char(2, "B", 5, 3, 2, 0, 0, 0),
                 char(3, "C", 5, 3, 2, 0, 0, 0)]
        chars[0].battle["max_chain"], chars[1].battle["max_chain"] = 4, 9
        recs = client_parse(web.ranking_body("chain", "j", chars), "chain")
        self.assertEqual([(r[1], r[3]) for r in recs], [(b"2", b"9"), (b"1", b"4")])

    def test_battle_round_trip(self):
        c = char(1, "A", 1, 1, 0, 0, 0, 0)
        c.battle["max_chain"] = 7
        self.assertEqual(model.Character.from_json(c.to_json()).battle["max_chain"], 7)
        self.assertEqual(model.Character.from_json({"id": 2, "name": "old"}).battle["games"], 0)

    def test_division(self):
        recs = client_parse(web.ranking_body("division", "j", CHARS), "division")
        self.assertEqual(recs[0], [b"1", "東京都".encode("euc_jp"), b"1000", b"2"])

    def test_bad_names_never_break_fields(self):
        c = char(9, "a\tb\nc", 1, 1, 0, 30, 0, 99)
        recs = client_parse(web.ranking_body("play", "j", [c]), "play")
        self.assertEqual(recs[0][2], b"a b c")

    def test_routes(self):
        self.assertEqual(web.ranking_request("/rankap/rank_dan.html"), ("dan", "j"))
        self.assertEqual(web.ranking_request("/e/division.html"), ("division", "e"))
        self.assertEqual(web.ranking_request("/j/point.html"), ("point", "j"))
        self.assertIsNone(web.ranking_request("/e/nope.html"))
        self.assertIsNone(web.ranking_request("/x/dan.html"))


class Http(unittest.TestCase):
    def test_round_trip(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for path in ("/e/dan.html", "/rankap/rank_point.html", "/whatever"):
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                conn.request("GET", path, headers={"User-Agent": "PUZZLEDAMA-PS2/KONAMISTD"})
                r = conn.getresponse()
                body = r.read()
                self.assertEqual(r.status, 200)
                self.assertEqual(int(r.getheader("Content-Length")), len(body))
                self.assertTrue(body.endswith(b"\n"))
                conn.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
