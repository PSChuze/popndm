"""Long commands shown as a few plain steps, with their full output in a log file."""
import queue
import re
import subprocess
import sys
import threading
import time


def minutes(seconds):
    m = int(seconds // 60)
    return "under a minute" if m < 1 else "1 minute" if m == 1 else "%d minutes" % m


class Progress:
    """One status line per step: "Getting the server program ready (about 2 minutes):
    1 minute so far", redrawn in place, and a closing line when the step ends.
    Without a terminal (output to a file) only the closing lines are written."""

    def __init__(self, out=None, clock=time.monotonic):
        self.out = out or sys.stdout
        self.clock = clock
        self.live = hasattr(self.out, "isatty") and self.out.isatty()
        self.title = None
        self.detail = ""
        self.started = 0.0
        self.width = 0

    def begin(self, title, estimate=""):
        if self.title == title:
            return
        self.end()
        self.title, self.estimate, self.detail, self.started = title, estimate, "", self.clock()
        if not self.live:
            self._write("%s%s...\n" % (title, " (%s)" % estimate if estimate else ""))
        self.tick()

    def set(self, detail):
        self.detail = detail
        self.tick()

    def line(self):
        text = "  " + self.title
        if self.estimate:
            text += " (%s)" % self.estimate
        parts = [p for p in (self.detail, "%s so far" % minutes(self.clock() - self.started)) if p]
        return text + ": " + ", ".join(parts)

    def tick(self):
        if self.live and self.title:
            text = self.line()
            self._write("\r" + text + " " * max(0, self.width - len(text)))
            self.width = len(text)

    def end(self, ok=True):
        if not self.title:
            return
        took = minutes(self.clock() - self.started)
        text = "  %s %s (%s)" % ("Done:" if ok else "Stopped:", self.title, took)
        if self.live:
            self._write("\r" + text + " " * max(0, self.width - len(text)) + "\n")
        else:
            self._write(text + "\n")
        self.title, self.width = None, 0

    def _write(self, text):
        self.out.write(text)
        self.out.flush()


def run_logged(cmd, log, on_line=None, progress=None, cwd=None, env=None):
    """Run cmd with its output going to the open file `log` (and to on_line,
    one line at a time) while the progress line keeps ticking. Returns
    (exit code, the last 30 lines)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    lines = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    tail = []
    while True:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if progress:
                progress.tick()
            continue
        if line is None:
            break
        log.write(line)
        log.flush()
        line = line.rstrip("\r\n")
        tail = (tail + [line])[-30:]
        if on_line:
            on_line(line)
    proc.stdout.close()
    return proc.wait(), tail


# What a failed step's last lines usually mean, in words a host can act on.
KNOWN_ERRORS = [
    (r"(?i)port is already allocated|address already in use|ports are not available",
     "Another program on this computer uses one of the server's ports (5730, 5731, 5732)."),
    (r"network openlobby.*not (?:be )?found",
     "The openlobby Docker network is missing. Run setup again; it makes the network."),
    (r"No space left on device|no space left", "The disk is full. Free some space and try again."),
    (r"(?i)urlopen error|name resolution|timed out|connection (reset|refused)|TLS handshake",
     "A download failed. Check the internet connection and try again."),
]


def explain(tail):
    """A plain sentence for a known failure in these output lines, or ""."""
    text = "\n".join(tail)
    for pattern, message in KNOWN_ERRORS:
        if re.search(pattern, text):
            return message
    return ""
