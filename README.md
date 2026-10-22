# popndm

A server for *pop'n Taisen Puzzle-dama Online* (PlayStation 2, SLPM-62464, Konami, 2004):
a reference implementation of the game's online service and a base for hosting your own.
It answers the gate, account and lobby server the game connects to
(`ps2pzd1.konamionline.com`), relays battles between consoles, and serves the in-game
rankings (`ps2pzdweb.konamionline.com`). Python standard library only, in Docker.

No game data is included and none is needed: the server speaks the game's protocol and
keeps its own accounts.

Players need a game install whose network settings and DNS point at this server. Battles
go through the server's relay when the install has the relay patch, so no player needs
to open a port for matches.

## Quick start

1. Download **pop'n Server Setup** for your computer from the [Releases](../../releases)
   page: `Popn-Server-Setup.exe` (Windows), `Popn-Server-Setup-mac.zip` (Mac with Apple
   silicon) or `Popn-Server-Setup-linux-x86_64`.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (on Linux,
   Docker Engine) and start it.
3. Double-click pop'n Server Setup and answer two questions: who will play (your home
   network, or friends over the internet) and whether the address it found is right.

Setup does the rest in a few minutes: it starts the server, starts the DNS server the
consoles use (or adds the game's names to the one already running here), asks your
router to open the ports if friends play over the internet, offers to connect an
OpenLobby admin panel if one runs on this computer, and ends with the line to send your
players:

```
Tell your players: set DNS 1 to 203.0.113.5 in the game's network settings
```

Run it again for a menu: start and stop, change who plays, update, backups, logs, DNS,
the admin panel, remove. From a clone of this repository, `python setup.py` (Python 3.8
or newer, nothing else to install) is the same program.

## By hand (without the setup program)

```sh
cp .env.example .env          # then set POPN_ADVERTISE to the IP address players reach
docker network create openlobby
docker compose up -d          # builds the image, starts the gate and the rankings
docker compose logs -f
```

The consoles also need a DNS server that answers `ps2pzd1.konamionline.com` and
`ps2pzdweb.konamionline.com` with your server's address (see DNS below).

### Ports

| port | what |
|---|---|
| 5731 TCP | gate, accounts, lobby and rooms (`ps2pzd1`) |
| 5730 TCP | battle relay |
| 5732 TCP | rankings (`ps2pzdweb`) |
| 80 TCP | rankings for stock installs, through the shared web server |
| 53 UDP | DNS, when this machine runs the resolver for players over the internet |

The game dials these numbers; they cannot be changed. For players outside your network,
forward them on your router (setup asks the router to do it with UPnP).

### DNS

A console has one DNS setting, so the game's two names have to resolve to your server
there. This repository ships no resolver of its own: it uses OpenLobby's `dns` service,
one per machine, shared by every game server on it. Setup looks for a running container
labelled `org.openlobby.role=dns`; if there is none it starts one from
[dns/compose.yml](dns/compose.yml) (project `openlobby-dns`). Then it adds the names:

```sh
docker exec <dns container> python stub.py dns-names add popn ps2pzd1.konamionline.com ps2pzdweb.konamionline.com
```

Everything else is forwarded upstream. To start the resolver by hand:

```sh
POL_ADVERTISE=192.0.2.10 docker compose -p openlobby-dns -f dns/compose.yml up -d
```

For friends over the internet, a resolver setup starts on Linux also answers internet
clients (`POL_DNS_PUBLIC=1`, for the added names only, so it is not an open resolver)
with your internet address. Any other DNS server you run works too, as long as it
answers both names with the address in `POPN_ADVERTISE`.

### Port 80

Installs with the ranking patch fetch the rankings from port 5732. A stock install asks
port 80 of `ps2pzdweb` for its rankings and its patch list. Port 80 is shared by every
game server on a machine, by host name, through OpenLobby's `http` service (label
`org.openlobby.role=http`). Setup reuses the one that runs, or starts it from
[http/compose.yml](http/compose.yml) (project `openlobby-http`) when port 80 is free, then
has it pass the host on:

```sh
docker exec <http container> python stub.py http-hosts add popn http://popn-web:5732 ps2pzdweb.konamionline.com
```

If another program holds port 80, setup says so and skips it; patched installs still
work. Nothing uses 443.

### Docker Desktop and player addresses

On Windows and macOS, Docker Desktop shows every connection as coming from one private
address. That changes three things:

- The gate cannot tell LAN players from internet players, so everyone gets
  `POPN_ADVERTISE` (setup leaves `POPN_ADVERTISE_MAP` empty there). With friends over
  the internet, players at home then use your internet address too, which needs a
  router that passes its own address back in.
- The battle relay pairs the two consoles of a match by their address. When every
  console has the same address, two matches started within a few minutes of each other
  can be paired wrongly. With more than one match at a time, run the server on Linux.
- Without the relay (`POPN_P2P_RELAY=0`), consoles connect to each other directly and
  need each other's real address: that works only on Linux.

On Linux, published ports keep each player's own address and all of this works as meant.

## Known limits

- **One console per character.** A character that is already in the lobby on one console
  cannot enter it from a second one: the second gets the game's "already logged in"
  message until the first has been silent for 15 seconds, and then takes its place. Two
  consoles can share a login id if each picks a different character.
- The game's unfinished puzzle mode (the greyed-out menu item) is not served.
- Three ranking pages (balls cleared, top characters, top attack balls) are empty.

## Admin panel

The gate has an admin port for an [OpenLobby](https://github.com/PrettyOpenLobby/OpenLobby)
admin panel: who is online and in which room, the rooms, Disconnect a player (with a
player picker), and a Rankings page. It is reached only over the Docker network
`openlobby`, as `http://popn:5738`, never published. The panel finds the server by its
labels (`org.openlobby.role=game`, `org.openlobby.game.key=popn`).

Setup pairs the two when a panel runs on the same machine: the panel shows a one-time
code, setup sends it with the admin token (`POPN_ADMIN_TOKEN`) and keeps the pairing
secret (`OPENLOBBY_PAIRING`). A panel that cannot pair yet takes
`POL_ADMIN_GAME_POPN_URL=http://popn:5738` and `POL_ADMIN_GAME_POPN_TOKEN` set to
`POPN_ADMIN_TOKEN`.

## Configuration

Settings live in `.env`; apply changes with `docker compose up -d`.

| setting | `.env.example` | what |
|---|---|---|
| `POPN_ADVERTISE` | 192.0.2.10 | the IP address the gate hands players (an IP, not a name) |
| `POPN_ADVERTISE_MAP` | empty | other addresses per player network: `cidr=ip,...`, first match wins |
| `POPN_P2P_RELAY` | 1 | battles go through the relay (installs with the relay patch) |
| `POPN_RELAY_HOST` | empty | the relay address handed out; empty = as the server list |
| `POPN_AUTH_OPEN` | 1 | an unknown login id is let in and becomes an account |
| `POPN_IDLE_TIMEOUT` | 120 | seconds of silence before a lobby connection is dropped |
| `POPN_LIVE_EVERY` | 10 | seconds between writes of `data/board/live.json`; 0 = off |
| `POPN_ADMIN_TOKEN` | empty | admin port token; empty keeps the admin port off |
| `POPN_STATE` | ./state | where accounts and logs are kept |

Accounts: the game's own New Account screen issues an id and password, which the server
keeps. With `POPN_AUTH_OPEN=1` a login with an id the server has not seen is also let in.

Website login links (`popn/gamelink.py`) are an optional OpenLobby website integration that lets
players link a game login to their website account. It stays off unless `GAMELINK_SECRET` is set.

## Backups

Everything players have (accounts, characters, standings) is in
`state/data/accounts.json`. Setup > "Make a backup" zips `state/data` into `backups/`;
by hand, copy that file while the server runs (it is replaced whole on every save).

The gate writes one log per connection to `state/logs/`; delete old ones when you like.

The server's logs and `accounts.json` hold player logins and passwords readably, so keep the
state folder private.

## Updating

Setup > "Update", or:

```sh
git pull
docker compose up -d --build
```

## Development

```sh
python -m unittest discover -s . -p "test_*.py" -t .
python -u -m popn.gate      # 0.0.0.0:5731, state in ./data
python -u -m popn.web       # set POPN_WEB_PORT=5732
```

`POPN_ELF` pointed at the disc's `SLPM_624.64` adds checks of the frame constants against
the game.

## Licence

popndm is free software under the GNU General Public License, version 3 or (at your
option) any later version: see [LICENSE](LICENSE). Third-party code and sources are listed in
[CREDITS.md](CREDITS.md).

pop'n Taisen Puzzle-dama Online, pop'n and Konami are trademarks of Konami Digital
Entertainment. PlayStation is a trademark of Sony Interactive Entertainment. This project is
not affiliated with or endorsed by either. All other trademarks belong to their owners.
