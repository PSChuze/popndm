"""adminport.py - the gate in an OpenLobby admin panel (the panel's admin protocol, olport.py).

Off unless POPN_ADMIN_PORT is set. The panel is then given this address and the token:

  POPN_ADMIN_PORT        the HTTP port, e.g. 5738
  POPN_ADMIN_BIND        default 127.0.0.1; the panel's container reaches the host-networked gate
                         through the Docker bridge, so a host-networked gate binds 172.17.0.1 (a token
                         is then required)
  POPN_ADMIN_TOKEN       the token the panel sends (POL_ADMIN_GAME_POPN_TOKEN on the panel's side)
  POPN_ADMIN_CONTAINERS  the containers its server check covers (comma list), default this
                         compose file's (project "popn")

What the panel gets: who is in the lobby and in which room, the rooms, and the totals, all from
live.snapshot (the same dict the boards read from board/live.json). One action, Disconnect a
player, which an account's Kick uses: it closes that player's lobby connection the way the gate
already drops a stale session when the same character logs in again, and the gate's own cleanup
tells everyone else the player left (0x4401).

There is no Announce. The gate sends no system or notice line today: the 0x2001 notice is read
only once, at the first lobby entry, and a 0x4408 chat line from a uid outside the roster would
show as "???". So the panel's maintenance warning does not reach this game.

One tool page, Rankings: the in-game ranking tables (web.py builds the same ones for the
client), drawn from the gate's live state with the login id and online mark beside each
character. Balls cleared, top characters and top attack balls are left off, because the gate
serves those to the client empty. The page has no script, as the panel requires.

The panel's player picker (@port.players) finds characters by name or login id, the ones
online first; the kick action takes the character name it returns.

The account window's player lookup (@port.player) reads the links the panel sends: the pop'n
logins an OpenLobby account's members have proved are theirs on the website (the website's
game link, made through gamelink.py). Their key is the login id itself (gate.link_verify), the
key of model.STATE.accounts. Each of the login's characters is shown with its rank, prefecture
and record, and whether it is in the lobby now. Handles are never matched to character names.
"""
import os

from . import live, model, olpage, olport, web

PORT = int(os.environ.get("POPN_ADMIN_PORT", "0") or 0)
BIND = os.environ.get("POPN_ADMIN_BIND", "127.0.0.1")
TOKEN = os.environ.get("POPN_ADMIN_TOKEN", "")
CONTAINERS = [c.strip() for c in os.environ.get(
    "POPN_ADMIN_CONTAINERS", "popn-gate-1,popn-web-1").split(",") if c.strip()]


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def status_doc(lobby):
    """live.snapshot(lobby) in the panel's status shape."""
    snap = live.snapshot(lobby)
    room_names = {r["id"]: r["name"] for r in snap["rooms"]}
    playing = [r for r in snap["rooms"] if r["in_battle"]]
    return {
        "up": True, "online": snap["online"],
        "players": [{"name": p["name"],
                     "where": room_names.get(p["room"], "a room") if p["room"] is not None else "lobby"}
                    for p in snap["players"]],
        "rooms": [{"name": r["name"], "players": len(r["players"]), "max": model.ROOM_CAPACITY,
                   "state": "playing" if r["in_battle"] else "locked" if r["locked"] else "open"}
                  for r in snap["rooms"]],
        "rooms_label": "rooms",
        "summary": "%s playing" % _plural(len(playing), "match") if playing else "",
        "facts": [{"label": "ranked characters", "value": snap["totals"]["characters"]},
                  {"label": "matches played", "value": snap["totals"]["matches"]}],
    }


def disconnect(lobby, names, actor="", log=print):
    """Close the lobby connection of every player whose character name or login id is in `names`
    (any case). -> the character names disconnected."""
    want = {n.strip().casefold() for n in names if n.strip()}
    hit = []
    with model.STATE.lock:
        for uid, s in list(lobby.items()):
            c = model.STATE.character(uid)
            keys = {(c.name if c else "").casefold(), (s.account_key or "").casefold()} - {""}
            if keys & want:
                hit.append((s, c.name if c else str(uid)))
    for s, name in hit:
        s.note("   disconnected from the admin panel" + (" by %s" % actor if actor else ""))
        s.close()
    if hit:
        log("adminport: %s disconnected %s" % (actor or "panel", ", ".join(n for _s, n in hit)))
    return [n for _s, n in hit]


def player_doc(lobby, who):
    """The panel's player lookup: the characters of every linked pop'n login (who["links"]["popn"],
    keys = login ids), or None when the gate knows none of them. Read under the state lock; never
    the password or the 0x4100 profile (postal code, birthday)."""
    keys = [x.get("key", "") for x in (who.get("links") or {}).get("popn", [])]
    chars = []
    with model.STATE.lock:
        online = set(lobby)
        for key in dict.fromkeys(k for k in keys if k):
            acct = model.STATE.accounts.get(key)    # not STATE.account(): that one creates
            if acct is None:
                continue
            for c in acct.characters:
                s = c.stats
                chars.append({
                    "name": c.name,
                    "detail": "login %s, rank %d, %s" % (key, c.flags[model.RANK],
                                                         web.pref_name(c.flags[model.PREF], "e")),
                    "online": c.id in online,
                    "facts": [{"label": "matches", "value": s[model.PLAYS]},
                              {"label": "won / lost", "value": "%d / %d" % (s[model.WINS], s[model.LOSSES])},
                              {"label": "points", "value": s[model.POINTS]},
                              {"label": "high score", "value": s[model.HISCORE]}]})
            if not acct.characters:
                chars.append({"name": key, "detail": "login %s, no character yet" % key,
                              "online": False, "facts": []})
    return {"characters": chars} if chars else None


def players_doc(lobby, q, limit):
    """The panel's player picker: characters whose name or login id holds `q` (any case),
    online first, at most `limit`. An empty `q` lists the characters online now. The id
    is the character name, which the kick action takes."""
    q = q.strip().casefold()
    rows = []
    with model.STATE.lock:
        online = set(lobby)
        for acct in model.STATE.accounts.values():
            for c in acct.characters:
                on = c.id in online
                if q and q not in c.name.casefold() and q not in acct.key.casefold():
                    continue
                if not q and not on:
                    continue
                rows.append(((not on, c.name.casefold(), c.id),
                             {"id": c.name, "label": c.name, "online": on,
                              "detail": "login %s, rank %d" % (acct.key, c.flags[model.RANK])}))
    rows.sort(key=lambda r: r[0])
    return [row for _key, row in rows[:limit]]


# (category, table title, value heading): web.SERVED less division, which has its own table
RANKING_TABLES = (("dan", "Rank", "Points"), ("point", "Points", "Points"), ("kati", "Wins", "Wins"),
                  ("lose", "Losses", "Losses"), ("pwin", "Win rate", "Win rate"),
                  ("play", "Matches played", "Matches"), ("chain", "Max chain", "Chain"),
                  ("score", "High score", "Score"))
assert {t[0] for t in RANKING_TABLES} == set(web.CHARACTER_CATEGORIES)


def ranking_tables(lobby):
    """({category: [(place, name, login id, value, rank, prefecture index, online)]},
    [(place, prefecture index, points, players)]), read under the state lock."""
    with model.STATE.lock:
        login = {c.id: a.key for a in model.STATE.accounts.values() for c in a.characters}
        chars = [c for a in model.STATE.accounts.values() for c in a.characters]
        online = set(lobby)
        tables = {cat: [(place, c.name, login[c.id], value, c.flags[model.RANK],
                         c.flags[model.PREF], c.id in online)
                        for place, c, value in web.character_standings(cat, chars)]
                  for cat in web.CHARACTER_CATEGORIES}
        division = web.division_standings(chars)
    return tables, division


def _value(cat, v):
    return "%.2f%%" % (v / 100.0) if cat == "pwin" else v


def rankings_page(lobby):
    tables, division = ranking_tables(lobby)
    mono = lambda v: olpage.Raw('<span class="mono">%s</span>' % olpage.esc(v))
    on = olpage.Raw('<span class="pill">online</span>')
    panels = []
    for cat, title, heading in RANKING_TABLES:
        rows = [[place, name, mono(key), _value(cat, value), rank, web.pref_name(pref, "e"),
                 on if online else ""]
                for place, name, key, value, rank, pref, online in tables[cat]]
        panels.append(olpage.panel(title, olpage.table(
            ["#", "Character", "Login id", heading, "Rank", "Prefecture", ""], rows,
            empty="No one has played yet.")))
    panels.append(olpage.panel("Prefectures", olpage.table(
        ["#", "Prefecture", "Points", "Players"],
        [[place, web.pref_name(p, "e"), pts, n] for place, p, pts, n in division],
        empty="No one has played yet.")))
    return olpage.page(
        "Rankings", *panels,
        sub="The ranking pages the game shows, top %d each, as they stand now. Only characters "
            "that have played are listed." % web.MAX_ROWS)


def build(lobby, log=print):
    """The AdminPort for the gate's LOBBY (gate.LOBBY); not started."""
    port = olport.AdminPort(
        "popn", "pop'n Puzzle-dama Online", short="pop'n", platform="PS2 HDD",
        about="Lobby, two-player rooms and ranked matches; battles run console to console "
              "through the relay.",
        token=TOKEN, bind=BIND, port=PORT,
        services=[{"name": "pop'n Puzzle-dama Online", "containers": CONTAINERS,
                   "ports": {"5731": "game", "5730": "battle relay", "5732": "rankings"},
                   "warning": "Everyone in pop'n is disconnected, a match in progress too."}])

    @port.status
    def status():
        return status_doc(lobby)

    @port.player
    def player(who):
        return player_doc(lobby, who)

    @port.players
    def players(q, limit):
        return players_doc(lobby, q, limit)

    @port.action("kick", "Disconnect a player", kind="kick", danger=True,
                 fields=[{"id": "name", "label": "Character or login id", "type": "player"}],
                 about="Closes the player's lobby connection, as the gate does to a stale "
                       "session. They can log in again at once.")
    def kick(params, actor):
        # from the panel's form a character name or login id; from an account's Kick its handles
        names = str(params.get("name") or params.get("handles") or "").split(",")
        if not any(n.strip() for n in names):
            return False, "No player named."
        gone = disconnect(lobby, names, actor, log)
        if not gone:
            return False, "Not online."
        return True, "Disconnected %s." % ", ".join(gone)

    @port.tool("rankings", "Rankings", "rankings",
               about="The in-game ranking tables, with each character's login id.")
    def rankings(method, sub, query, body, actor):
        if method != "GET":
            return 405, "text/plain; charset=utf-8", b"This page only shows the rankings."
        return 200, olpage.CTYPE, rankings_page(lobby).encode("utf-8")

    return port


def start(lobby, log=print):
    """Start the admin port if POPN_ADMIN_PORT is set. Never raises: a bad setting is logged and
    the gate runs on without it."""
    if not PORT:
        return None
    try:
        port = build(lobby, log)
        host, n = port.start()
    except (OSError, ValueError) as ex:
        log("adminport: not started (%s)" % ex)
        return None
    log("adminport: OpenLobby admin protocol on %s:%d" % (host, n) + ("" if TOKEN else " (no token)"))
    return port
