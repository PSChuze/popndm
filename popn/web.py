"""ps2pzdweb.konamionline.com -- the HTTP side (rankings + patch).

The stock client fetches, over plain HTTP (port 80):
    /rankap/rank_{dan,pwin,kati,lose,play,chain,dtama,division,chara,attack,score,point}.html
    /patch_pkg/pzdgetpflist.html     (the patch-package list)
    /patch_pkg/pzdgetpfile.html      (a patch file)
Port 80 is often taken by another server, so installs with the ranking patch fetch
the rankings from this server's own port instead, as
    /j/<cat>.html   (Japanese install: prefecture names in kanji)
    /e/<cat>.html   (English install: prefecture names in romaji)
The stock /rankap/ paths are still answered (as Japanese).

Rankings are built from the gate's accounts.json (read-only; the gate replaces it
atomically on every save). Body format, from the client's parser: TSV, EUC-JP, one
record per line, and the record count is the number of '\n' minus one, so every body
ends with an extra '\n' ("\n" alone = an empty table; an empty body makes the client
malloc(-0x38)). Any fetch failure or 10 s timeout is error -9999.

Default record: rank \t id \t name \t value \t dan \t prefecture \n
    id = the character id (the uid; with the name it highlights the player's own row),
    value = "%8d", dan = 0..17 (indexes the client's own dan-name table),
    prefecture <= 8 bytes. Division: prefecture \t v1 \t v2 \n.
Max chain comes from the best 0x4508 +16 per character. Balls cleared (dtama), top
characters and top attack balls stay empty until their meaning is confirmed; the
gate already collects the 0x4508 totals they will be built from. The patch list is
answered empty = "no patch" (the gate never advertises a newer version).

Env: POPN_WEB_BIND (0.0.0.0), POPN_WEB_PORT (80), POPN_LOG_DIR (./data/logs),
     POPN_STATE_DB (./data/accounts.json).
"""
import datetime
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import model

RANKINGS = {
    "dan", "pwin", "kati", "lose", "play", "chain",
    "dtama", "division", "chara", "attack", "score", "point",
}
MAX_ROWS = 100           # the client pages 10 per screen; keep the download small
NAME_BYTES = 16          # record +0x08 is str[17]
PREF_BYTES = 8           # record +0x2f is str[9]

# 47 prefectures in the client's order (f2 = index). English names are cut to fit
# the 8-byte slot.
PREFS_JA = (
    "北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 "
    "東京都 神奈川県 新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 "
    "滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 "
    "香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()
PREFS_EN = (
    "Hokkaido Aomori Iwate Miyagi Akita Yamagata Fukushma Ibaraki Tochigi Gunma Saitama "
    "Chiba Tokyo Kanagawa Niigata Toyama Ishikawa Fukui Yamanshi Nagano Gifu Shizuoka "
    "Aichi Mie Shiga Kyoto Osaka Hyogo Nara Wakayama Tottori Shimane Okayama Hiroshma "
    "Yamagchi Tokushma Kagawa Ehime Kochi Fukuoka Saga Nagasaki Kumamoto Oita Miyazaki "
    "Kagoshma Okinawa").split()
assert len(PREFS_JA) == len(PREFS_EN) == 47


def log_dir():
    d = os.environ.get("POPN_LOG_DIR", os.path.join("data", "logs"))
    os.makedirs(d, exist_ok=True)
    return d


def state_path():
    return os.environ.get("POPN_STATE_DB", os.path.join("data", "accounts.json"))


_cache = {"key": None, "chars": []}
_cache_lock = threading.Lock()


def load_characters(path=None):
    """Every character in accounts.json as a model.Character, re-read when the file
    changes. A missing or half-written file gives the last good list (or none)."""
    path = path or state_path()
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (path, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["chars"]
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            chars = [model.Character.from_json(c)
                     for a in d.get("accounts", {}).values()
                     for c in a.get("characters", [])]
        except (OSError, ValueError, KeyError, TypeError):
            return _cache["chars"]
        _cache["key"], _cache["chars"] = key, chars
        return chars


def euc(text, limit):
    """`text` as EUC-JP within `limit` bytes, never cut mid-character, and never empty
    or holding a TAB/LF (strtok would merge or split the fields)."""
    text = "".join(" " if c in "\t\r\n" else c for c in str(text)).strip() or "?"
    out = b""
    for ch in text:
        b = ch.encode("euc_jp", "replace")
        if len(out) + len(b) > limit:
            break
        out += b
    return out or b"?"


def pref_name(i, lang):
    names = PREFS_EN if lang == "e" else PREFS_JA
    return names[i] if 0 <= i < len(names) else names[0]


def _standings(rows):
    """[(sort key, row)] sorted descending -> [(place, row)], ties share a place."""
    rows.sort(key=lambda kr: kr[0], reverse=True)
    out, place, prev = [], 0, object()
    for n, (k, row) in enumerate(rows[:MAX_ROWS], 1):
        if k != prev:
            place, prev = n, k
        out.append((place, row))
    return out


CHARACTER_CATEGORIES = ("dan", "pwin", "kati", "lose", "play", "chain", "score", "point")
# dtama / chara / attack: what the client showed is not pinned yet (the 0x4508 totals are
# being collected in Character.battle and ServerState.chara), so they are served empty.
SERVED = CHARACTER_CATEGORIES + ("division",)


def division_standings(chars):
    """[(place, prefecture index, points, players)]: every prefecture with a player who has
    played, by total points, then by players."""
    tot = {}
    for c in chars:
        if c.stats[model.PLAYS] > 0:
            p = c.flags[model.PREF]
            pts, n = tot.get(p, (0, 0))
            tot[p] = (pts + c.stats[model.POINTS], n + 1)
    return [(place, p, pts, n) for place, (p, (pts, n))
            in _standings([((pts, n), (p, (pts, n))) for p, (pts, n) in tot.items()])]


def character_standings(cat, chars):
    """[(place, character, value)] for one of CHARACTER_CATEGORIES. pwin's value is the win
    rate in hundredths of a percent; dan's is the points (the place is by rank, then points)."""
    s = lambda c, i: c.stats[i]
    key = {
        "chain": lambda c: (c.battle.get("max_chain", 0),),
        "dan":   lambda c: (c.flags[model.RANK], s(c, model.POINTS)),
        "pwin":  lambda c: (s(c, model.WINS) * 10000 // s(c, model.PLAYS), s(c, model.PLAYS)),
        "kati":  lambda c: (s(c, model.WINS),),
        "lose":  lambda c: (s(c, model.LOSSES),),
        "play":  lambda c: (s(c, model.PLAYS),),
        "score": lambda c: (s(c, model.HISCORE),),
        "point": lambda c: (s(c, model.POINTS),),
    }[cat]
    pool = [c for c in chars if c.stats[model.PLAYS] > 0
            and (key(c)[0] > 0 or cat not in ("chain", "score"))]
    return [(place, c, s(c, model.POINTS) if cat == "dan" else key(c)[0])
            for place, c in _standings([(key(c), c) for c in pool])]


def ranking_body(cat, lang="j", chars=None):
    """The TSV body for one ranking category (bytes, always ending in an extra '\\n')."""
    chars = load_characters() if chars is None else chars
    lines = []
    if cat == "division":
        for place, p, pts, n in division_standings(chars):
            lines.append(b"%d\t%s\t%d\t%d" % (place, euc(pref_name(p, lang), NAME_BYTES), pts, n))
    elif cat in CHARACTER_CATEGORIES:
        for place, c, value in character_standings(cat, chars):
            lines.append(b"%d\t%d\t%s\t%d\t%d\t%s" % (
                place, c.id, euc(c.name, NAME_BYTES), value,
                min(max(c.flags[model.RANK], 0), 17),
                euc(pref_name(c.flags[model.PREF], lang), PREF_BYTES)))
    return b"".join(l + b"\n" for l in lines) + b"\n"


def ranking_request(path):
    """(category, lang) for a ranking URL, or None."""
    path = path.split("?", 1)[0]
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".html"):
        return None
    stem = name[:-len(".html")]
    if path.startswith("/rankap/rank_"):
        cat, lang = stem[len("rank_"):], "j"
    elif path[:3] in ("/j/", "/e/") and path.count("/") == 2:
        cat, lang = stem, path[1]
    else:
        return None
    return (cat, lang) if cat in RANKINGS else None


class Handler(BaseHTTPRequestHandler):
    server_version = "popn-web/1"
    protocol_version = "HTTP/1.0"   # the client speaks 1.0 and expects the close

    def _log(self, body=b"", sent=None):
        line = (f"{datetime.datetime.now().isoformat(timespec='milliseconds')} "
                f"{self.client_address[0]} {self.command} {self.path} "
                f"host={self.headers.get('Host')} len={len(body)}"
                + (f" sent={sent}" if sent is not None else "") + "\n")
        try:
            with open(os.path.join(log_dir(), "web.log"), "a", encoding="utf-8") as fh:
                fh.write(line)
                if body:
                    fh.write("  body: " + body[:512].hex() + "\n")
        except OSError:
            pass
        try:
            print(line.rstrip(), flush=True)
        except UnicodeEncodeError:
            print(line.rstrip().encode("ascii", "replace").decode("ascii"), flush=True)

    def _body_for(self, path):
        r = ranking_request(path)
        if r:
            return ranking_body(*r)
        if path.startswith("/patch_pkg/"):
            return b""            # no patch
        # Status codes are ignored by the client, so anything else gets a body that
        # parses as an empty ranking rather than an error page parsed as data.
        return b"\n"

    def _respond(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, req_body=b""):
        try:
            body = self._body_for(self.path)
        except Exception as e:  # never let a bad record cost the player a -9999
            print(f"web: {self.path}: {e!r}", flush=True)
            body = b"\n"
        self._log(req_body, len(body))
        self._respond(body)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._serve(self.rfile.read(n) if n else b"")

    def log_message(self, *a):
        pass  # we log ourselves


def main():
    bind = os.environ.get("POPN_WEB_BIND", "0.0.0.0")
    port = int(os.environ.get("POPN_WEB_PORT", "80"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    print(f"popn web (ps2pzdweb) on {bind}:{port}, state {state_path()}, "
          f"logs in {log_dir()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
