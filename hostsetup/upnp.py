"""Ask the home router to forward ports to this machine (UPnP IGD).

Most home routers take these requests from machines on their network unless
UPnP is turned off in their settings. Three steps: find the router by an SSDP
search on the local network, read its description to learn the address of its
WANIPConnection (or WANPPPConnection) service, then send SOAP requests to it.
"""
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SSDP = ("239.255.255.250", 1900)
SEARCH_TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                  "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
                  "urn:schemas-upnp-org:service:WANIPConnection:1")
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
            "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")
CONFLICT = 718          # the port is forwarded to another machine already
WEEK = 7 * 24 * 3600


class UpnpError(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


def search_message(target, wait=2):
    return ("M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: %d\r\n"
            "ST: %s\r\n\r\n" % (wait, target)).encode("ascii")


def parse_reply(data):
    """The headers of an SSDP reply, names in lower case; None if it is not one."""
    lines = data.decode("latin-1").split("\r\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def discover(lan_ip="", timeout=3.0):
    """The description URLs of the gateways that answer, in the order they did."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if lan_ip:
            # Machines with Docker or a VPN have several networks: ask on the one
            # that leads to the router.
            s.bind((lan_ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan_ip))
        for target in SEARCH_TARGETS:
            s.sendto(search_message(target), SSDP)
        s.settimeout(timeout)
        found = []
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            headers = parse_reply(data) or {}
            location = headers.get("location")
            if location and location not in found:
                found.append(location)
        return found
    finally:
        s.close()


def _local(tag):
    return tag.rpartition("}")[2]


def _child_text(node, name):
    for c in node:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_description(xml_data, location):
    """[(service type, control URL)] of the connection services in a device
    description, best first."""
    root = ET.fromstring(xml_data)
    base = location
    for node in root.iter():
        if _local(node.tag) == "URLBase" and (node.text or "").strip():
            base = node.text.strip()
    found = []
    for node in root.iter():
        if _local(node.tag) != "service":
            continue
        kind, control = _child_text(node, "serviceType"), _child_text(node, "controlURL")
        if kind in SERVICES and control:
            found.append((kind, urllib.parse.urljoin(base, control)))
    found.sort(key=lambda f: SERVICES.index(f[0]))
    return found


def envelope(action, service, args):
    body = "".join("<%s>%s</%s>" % (k, _escape(str(v)), k) for k, v in args)
    return ('<?xml version="1.0"?>\r\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
            % (action, service, body, action)).encode("utf-8")


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_response(xml_data):
    """The values of a SOAP answer as {name: text}; raises UpnpError for a fault."""
    root = ET.fromstring(xml_data)
    for node in root.iter():
        if _local(node.tag) == "UPnPError":
            code = _child_text(node, "errorCode")
            raise UpnpError(_child_text(node, "errorDescription") or "error %s" % code,
                            int(code) if code.isdigit() else 0)
    for node in root.iter():
        if _local(node.tag) == "Body":
            for answer in node:
                return {_local(c.tag): (c.text or "").strip() for c in answer}
    raise UpnpError("the router's answer has no body")


class Gateway:
    def __init__(self, control_url, service, timeout=5.0):
        self.control_url, self.service, self.timeout = control_url, service, timeout

    @property
    def host(self):
        return urllib.parse.urlsplit(self.control_url).hostname or ""

    def call(self, action, args=()):
        req = urllib.request.Request(self.control_url, data=envelope(action, self.service, args), headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (self.service, action)})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return parse_response(r.read())
        except urllib.error.HTTPError as e:
            # Faults come back as HTTP 500 with the details in the body.
            try:
                body = e.read()
            except OSError:
                body = b""
            if body:
                parse_response(body)
            raise UpnpError("the router answered HTTP %d" % e.code)
        except (OSError, ET.ParseError) as e:
            raise UpnpError("no usable answer from the router (%s)" % e)

    def external_ip(self):
        return self.call("GetExternalIPAddress").get("NewExternalIPAddress", "")

    def mapping(self, port, protocol="TCP"):
        """(internal client, internal port) the router forwards port to, or None."""
        try:
            r = self.call("GetSpecificPortMappingEntry", [
                ("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol)])
        except UpnpError:
            return None
        return r.get("NewInternalClient", ""), int(r.get("NewInternalPort") or 0)

    def add(self, port, client, description, protocol="TCP", lease=0):
        self.call("AddPortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol),
            ("NewInternalPort", port), ("NewInternalClient", client), ("NewEnabled", 1),
            ("NewPortMappingDescription", description), ("NewLeaseDuration", lease)])

    def remove(self, port, protocol="TCP"):
        self.call("DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol)])


def find_gateway(lan_ip="", timeout=3.0, fetch=None):
    """The first router that offers port forwarding, or None."""
    fetch = fetch or _fetch
    for location in discover(lan_ip, timeout):
        try:
            services = parse_description(fetch(location), location)
        except (OSError, ET.ParseError, ValueError):
            continue
        if services:
            return Gateway(services[0][1], services[0][0])
    return None


def _fetch(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read()


def open_port(gateway, port, client, description, protocol="TCP"):
    """Forward one port to client: "opened", "already" (it was), or "taken"
    (another machine has it). Other refusals raise UpnpError."""
    have = gateway.mapping(port, protocol)
    if have and have[0] == client and have[1] == port:
        return "already"
    try:
        gateway.add(port, client, description, protocol)
    except UpnpError as e:
        if e.code == CONFLICT:
            return "taken"
        # Some routers only take forwards that expire; setup asks again at each start.
        gateway.add(port, client, description, protocol, lease=WEEK)
    return "opened"


def open_ports(gateway, ports, client, description="pop'n server"):
    """{(port, protocol): "opened" | "already" | "taken" | "refused: <why>"}, for ports
    given as numbers (TCP) or (port, protocol) pairs."""
    out = {}
    for item in ports:
        port, protocol = item if isinstance(item, tuple) else (item, "TCP")
        try:
            out[(port, protocol)] = open_port(gateway, port, client, description, protocol)
        except UpnpError as e:
            out[(port, protocol)] = "refused: %s" % e
    return out


def is_private(ip):
    """True for addresses that are not on the internet itself: home networks
    and the shared range internet providers use (carrier-grade NAT)."""
    try:
        a, b = [int(x) for x in ip.split(".")[:2]]
    except ValueError:
        return False
    return (a == 10 or (a == 172 and 16 <= b < 32) or (a == 192 and b == 168)
            or (a == 100 and 64 <= b < 128) or a == 127 or (a == 169 and b == 254))
