"""TCP relay for pop'n battles, so players need no port forwarding.

Battles are console-to-console over TCP 5730, and in the stock game one side always
LISTENS (the room owner first, the guest on the retry), so one player needs inbound
5730. Installs with the relay patch make both consoles take the connector path
instead: each waits 45 frames and connects out to the address the server put in
0x440b. The gate puts THIS relay's address there and registers each player's source
IP against the match (Relay.expect, on 0x4409); the relay pairs the two connections
that arrive for the same match and copies bytes both ways. The game exchanges a
0x1e8-byte state block per frame in lockstep (send, then recv with a 1 s timeout),
so a partner that arrives within a second costs nothing.

A console is matched by its source IP, which is the address the gate saw, because the
0x440b relay address is chosen with the same advertise map as the 0x2002 server list
(same network path). Two players behind one public IP are still fine: both
registrations carry the same match key and the first two connections pair.

Env (read by gate.main): POPN_P2P_RELAY=1 to enable, POPN_RELAY_PORT (5730),
POPN_RELAY_HOST (address handed out; default = the per-source advertise address).
"""
import select
import socket
import threading
import time

EXPECT_TTL = 180.0   # s a 0x4409 registration stays valid (covers the P2P retry round)
PAIR_WAIT = 30.0     # s a first connection waits for its partner


def _ip(addr):
    ip = addr[0]
    return ip[7:] if ip.startswith("::ffff:") else ip


def _alive(sock):
    """False if the waiting console already hung up (readable and EOF)."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return True
        return sock.recv(1, socket.MSG_PEEK) != b""
    except OSError:
        return False


class Relay:
    def __init__(self, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.expected = []        # [(match_key, ip, deadline)]
        self.waiting = {}         # match_key -> (sock, ip)
        self.pairs = 0

    def expect(self, match_key, ip):
        """Register that the console at `ip` will connect for `match_key` (gate, 0x4409)."""
        now = time.time()
        with self.lock:
            self.expected = [e for e in self.expected if e[2] > now and e[:2] != (match_key, ip)]
            self.expected.append((match_key, ip, now + EXPECT_TTL))

    def _key_for(self, ip):
        now = time.time()
        for key, eip, deadline in reversed(self.expected):
            if eip == ip and deadline > now:
                return key
        return None

    def handle(self, sock, addr):
        ip = _ip(addr)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            key = self._key_for(ip)
            if key is None:
                partner = None
            else:
                partner = self.waiting.pop(key, None)
                if partner is not None and not _alive(partner[0]):
                    self.log(f"relay: dropping dead waiter {partner[1]} for {key}")
                    partner[0].close()
                    partner = None
                if partner is None:
                    self.waiting[key] = (sock, ip)
        if key is None:
            self.log(f"relay: {ip} connected with no match registered; closing")
            sock.close()
            return
        if partner is None:
            self.log(f"relay: {ip} waiting for its partner ({key})")
            threading.Thread(target=self._expire, args=(key, sock), daemon=True).start()
            return
        with self.lock:
            self.pairs += 1
        self.log(f"relay: paired {partner[1]} <-> {ip} ({key})")
        self.splice(partner[0], sock)

    def _expire(self, key, sock):
        time.sleep(PAIR_WAIT)
        with self.lock:
            if self.waiting.get(key, (None,))[0] is sock:
                del self.waiting[key]
            else:
                return
        self.log(f"relay: partner never arrived for {key}; closing")
        sock.close()

    @staticmethod
    def splice(a, b):
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(b, a), daemon=True)
        t.start()
        pump(a, b)
        t.join()
        a.close()
        b.close()

    def serve(self, bind, port):
        srv = socket.create_server((bind, port), reuse_port=False)
        self.log(f"popn relay on {bind}:{port}")
        self.serve_on(srv)

    def serve_on(self, srv):
        while True:
            try:
                sock, addr = srv.accept()
            except OSError:
                return              # listening socket closed (shutdown / tests)
            threading.Thread(target=self.handle, args=(sock, addr), daemon=True).start()
