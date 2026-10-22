"""The gate's public snapshot for status boards: who is online, the open
rooms, the last few match results and the standings, written to
POPN_LIVE_FILE (default <state dir>/board/live.json) every POPN_LIVE_EVERY
seconds (10).

The board runs in its own container with ONLY that board/ directory mounted read-only: it
never connects to the gate (a board bug can never touch a live session) and never sees
accounts.json (which holds passwords). So everything the board draws is published here,
and nothing else: character names and their public stats, the same things the in-game
ranking pages show.

Shape (version 1):
    {"v": 1, "game": "popn", "updated": unix, "online": N,
     "players": [{"id", "name", "rank", "room"}],          # room id or null
     "rooms": [{"id", "name", "players": [names], "locked", "in_battle"}],
     "matches": [{"n", "at", "players": [{"name", "rank", "outcome", "points"}]}],
     "standings": {category: [{"place", "id", "name", "rank", "value", "plays", "wins",
                               "losses"}]},                 # top STANDINGS_ROWS each
     "ranks": {char id: rank}, "names": {char id: name},   # every ranked character
     "totals": {"characters", "matches"}}
    matches are newest first; n counts matches since the gate started (two can share a
    second). Standing categories: rank (rank, then points; value = points), points, wins,
    rate (win %, >= RATE_MIN_PLAYS matches; value = percent, 1 decimal), chain, score.
"""
import json
import os
import threading
import time

from . import model

VERSION = 1
MATCHES_KEPT = 20
STANDINGS_ROWS = 25
RATE_MIN_PLAYS = 10
_matches = []           # newest first; guarded by model.STATE.lock
_seq = [0]


def path(state_db):
    return os.environ.get("POPN_LIVE_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(state_db)), "board", "live.json")


def _room_name(room):
    raw = room.name if isinstance(room.name, bytes) else str(room.name).encode("cp932", "replace")
    return model.printable_cp932(raw) or "?"


def record_match(changes):
    """Remember one finished match. `changes` = apply_match_result's {uid: (outcome,
    points delta, new rank)}. Call with model.STATE.lock held."""
    players = []
    for uid, (outcome, delta, rank) in changes.items():
        c = model.STATE.character(uid)
        players.append({"name": c.name if c else "?", "rank": rank,
                        "outcome": outcome, "points": delta})
    players.sort(key=lambda p: p["outcome"] != "win")
    _seq[0] += 1
    _matches.insert(0, {"n": _seq[0], "at": int(time.time()), "players": players})
    del _matches[MATCHES_KEPT:]


def _standing_key(cat):
    """(sort key, shown value) of a standings category."""
    s = lambda c, i: c.stats[i]
    return {
        "rank":   (lambda c: (c.flags[model.RANK], s(c, model.POINTS)), lambda c: s(c, model.POINTS)),
        "points": (lambda c: (s(c, model.POINTS),), lambda c: s(c, model.POINTS)),
        "wins":   (lambda c: (s(c, model.WINS),), lambda c: s(c, model.WINS)),
        "rate":   (lambda c: (s(c, model.WINS) * 1000 // s(c, model.PLAYS), s(c, model.PLAYS)),
                   lambda c: round(s(c, model.WINS) * 100.0 / s(c, model.PLAYS), 1)),
        "chain":  (lambda c: (c.battle.get("max_chain", 0),), lambda c: c.battle.get("max_chain", 0)),
        "score":  (lambda c: (s(c, model.HISCORE),), lambda c: s(c, model.HISCORE)),
    }[cat]


STANDINGS = ("rank", "points", "wins", "rate", "chain", "score")


def standings(chars):
    """{category: top STANDINGS_ROWS}, ties sharing a place. Only characters that have
    played; a category whose value is 0 leaves them out (except rank)."""
    played = [c for c in chars if c.stats[model.PLAYS] > 0]
    out = {}
    for cat in STANDINGS:
        key, value = _standing_key(cat)
        pool = [c for c in played if cat == "rank" or key(c)[0] > 0]
        if cat == "rate":
            pool = [c for c in pool if c.stats[model.PLAYS] >= RATE_MIN_PLAYS]
        pool.sort(key=lambda c: (key(c), -c.id), reverse=True)
        rows, place, prev = [], 0, None
        for n, c in enumerate(pool[:STANDINGS_ROWS], 1):
            if key(c) != prev:
                place, prev = n, key(c)
            rows.append({"place": place, "id": c.id, "name": c.name, "rank": c.flags[model.RANK],
                         "value": value(c), "plays": c.stats[model.PLAYS],
                         "wins": c.stats[model.WINS], "losses": c.stats[model.LOSSES]})
        out[cat] = rows
    return out


def snapshot(lobby, now=None):
    """The public dict, built under the state lock from the gate's LOBBY and STATE."""
    with model.STATE.lock:
        players = []
        for uid, s in sorted(lobby.items()):
            c = model.STATE.character(uid)
            players.append({"id": uid, "name": c.name if c else "?",
                            "rank": c.flags[model.RANK] if c else 0,
                            "room": s.room.id if getattr(s, "room", None) else None})
        names = {p["id"]: p["name"] for p in players}
        rooms = []
        for r in model.STATE.room_list():
            if not r.members and r.seed:
                continue      # the test knob's empty seed room is not a game anyone is in
            rooms.append({"id": r.id, "name": _room_name(r),
                          "players": [names.get(m, "?") for m in r.members],
                          "locked": bool(r.password), "in_battle": bool(r.in_battle)})
        chars = [c for a in model.STATE.accounts.values() for c in a.characters]
        played = [c for c in chars if c.stats[model.PLAYS] > 0]
        return {"v": VERSION, "game": "popn",
                "updated": int(time.time() if now is None else now),
                "online": len(players), "players": players, "rooms": rooms,
                "matches": list(_matches), "standings": standings(chars),
                "ranks": {str(c.id): c.flags[model.RANK] for c in played},
                "names": {str(c.id): c.name for c in played},
                "totals": {"characters": len(played),
                           "matches": sum(c.stats[model.PLAYS] for c in chars) // 2}}


def write(dest, snap):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, dest)


def start(lobby, state_db):
    """Publish every POPN_LIVE_EVERY seconds on a daemon thread. POPN_LIVE_EVERY=0 = off."""
    every = float(os.environ.get("POPN_LIVE_EVERY", "10"))
    if every <= 0:
        return None
    dest = path(state_db)

    def loop():
        while True:
            try:
                write(dest, snapshot(lobby))
            except Exception as e:                     # noqa: BLE001 never kill the gate
                print(f"live: could not write {dest}: {e!r}", flush=True)
            time.sleep(every)

    t = threading.Thread(target=loop, name="live", daemon=True)
    t.start()
    print(f"live snapshot -> {dest} every {every:g}s", flush=True)
    return t
