"""Check a running server the way the game does: the gate's server list and a ranking page."""
import http.client
import socket

from popn import wire


def server_list(host, port=5731, timeout=5.0):
    """Send the client's hello (0x2000); -> the address in the 0x2002 answer."""
    dec = wire.Decoder()
    frames = []
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(wire.build(0x2000, 1))
        while not frames:
            data = s.recv(4096)
            if not data:
                raise OSError("the gate closed the connection")
            try:
                frames = dec.feed(data)
            except wire.FrameError as e:
                raise OSError("the gate's answer is not a frame (%s)" % e)
    f = frames[0]
    if f.opcode != 0x2002 or len(f.payload) < 51:
        raise OSError("the gate answered %#06x instead of its server list" % f.opcode)
    if not f.digest_ok:
        raise OSError("the gate's answer has a bad checksum")
    return f.payload[36:51].split(b"\0", 1)[0].decode("ascii", "replace")


def ranking(host, port=5732, timeout=5.0):
    """Fetch one ranking page; True when it has the shape the game reads."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/e/dan.html")
        r = conn.getresponse()
        body = r.read()
    finally:
        conn.close()
    return r.status == 200 and body.endswith(b"\n")


def check(host="127.0.0.1", gate_port=5731, web_port=5732):
    """-> (ok, a sentence for the host)."""
    try:
        advertised = server_list(host, gate_port)
    except OSError as e:
        return False, "The gate on port 5731 does not answer as it should (%s)." % e
    try:
        if not ranking(host, web_port):
            return False, "The rankings on port 5732 answer, but not as the game expects."
    except OSError as e:
        return False, "The rankings on port 5732 do not answer (%s)." % e
    return True, "The server answers as it should; it gives players the address %s." % advertised
