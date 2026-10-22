# SPDX-License-Identifier: AGPL-3.0-or-later OR GPL-3.0-or-later
# Copyright (C) OpenLobby contributors. This file alone may be used under either licence,
# so a GPL-3.0 game server can carry a copy of it unchanged. See LICENSE for the AGPL.
"""olpage.py -- small HTML pages for a game's admin tools, in the admin panel's own look.

A game's tool page (olport.AdminPort.tool) is shown inside the panel, so it should read as
part of it: the same colours in light and dark, the same tables, buttons and forms. This
module is the panel's look in one stylesheet plus a few builders; everything a page puts on
screen goes through esc() or one of the builders, which escape it.

    from olpage import page, table, form, esc

    def auction(method, sub, query, body, actor):
        rows = [[r["card"], r["seller"], r["price"]] for r in listings()]
        return 200, page.CTYPE, page("Auction house",
            table(["Card", "Seller", "Price"], rows), note=msg).encode()

The panel serves tool pages under a strict script policy, so these pages use no script at
all: a form posts back to the same path and the answer is the page again, with a note.
"""
import html
import urllib.parse

CTYPE = "text/html; charset=utf-8"

CSS = """
:root{--bg:#0f1216;--panel:#161a20;--panel2:#1d222a;--line:#2a303b;--text:#e6e9ef;--muted:#9aa3b2;
--accent:#6ea6ff;--on-accent:#04121f;--good:#43c08f;--warn:#e5a63e;--bad:#f2736b;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#f4f5f8;--panel:#fff;--panel2:#eef0f4;--line:#dde1e8;
--text:#161a21;--muted:#566070;--accent:#1f5fd6;--on-accent:#fff;--good:#0f7a54;--warn:#935800;
--bad:#c13a32;color-scheme:light}}
*{box-sizing:border-box}
body{margin:0;padding:16px;font:14px/1.45 'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text)}
h1{font-size:17px;margin:0 0 4px;font-weight:600}
h2{font-size:15px;margin:0 0 10px;font-weight:600}
p.sub{margin:0 0 14px;color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 16px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.tbox{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;font-weight:500;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.note{border-radius:8px;padding:10px 12px;margin:0 0 16px;background:var(--panel2)}
.note.ok{color:var(--good)} .note.bad{color:var(--bad)}
form.row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin:0}
form.inline{display:inline;margin:0}
label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
input,select{font:inherit;font-size:14px;color:var(--text);background:var(--panel2);border:1px solid var(--line);
border-radius:8px;padding:7px 10px;min-height:36px}
button{font:inherit;font-weight:500;min-height:34px;padding:0 14px;border-radius:8px;border:1px solid var(--line);
background:var(--panel2);color:var(--text);cursor:pointer}
button.act{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
button.danger{color:var(--bad)}
dl.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:0;font-size:13px}
dl.kv dt{color:var(--muted)} dl.kv dd{margin:0}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--panel2);color:var(--muted)}
"""


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


class Raw(str):
    """Markup a builder made; table() and kv() put it in unescaped."""


def _cell(v):
    return v if isinstance(v, Raw) else esc(v)


def page(title, *parts, sub="", note=None):
    """A whole page. `note` is (ok, text) to show above the content, or None."""
    top = "<h1>%s</h1>%s" % (esc(title), "<p class=sub>%s</p>" % esc(sub) if sub else "")
    msg = ""
    if note:
        ok, text = note
        msg = '<div class="note %s">%s</div>' % ("ok" if ok else "bad", esc(text))
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,"
            "initial-scale=1'><title>%s</title><style>%s</style>%s%s%s"
            % (esc(title), CSS, top, msg, "".join(parts)))


def panel(title, *parts):
    return Raw('<section class="panel">%s%s</section>' % ("<h2>%s</h2>" % esc(title) if title else "",
                                                          "".join(parts)))


def grid(*panels):
    """Panels side by side where there is room."""
    return Raw('<div class="grid">%s</div>' % "".join(panels))


def table(heads, rows, empty="Nothing here."):
    if not rows:
        return Raw('<p class="muted">%s</p>' % esc(empty))
    head = "".join("<th>%s</th>" % esc(h) for h in heads)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _cell(c) for c in r) for r in rows)
    return Raw('<div class="tbox"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
               % (head, body))


def kv(pairs):
    return Raw('<dl class="kv">%s</dl>' % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), _cell(v))
                                                  for k, v in pairs))


def form(fields, button, hidden=None, danger=False, inline=False, confirm=""):
    """A POST form back to this page. `fields`: [(name, label, value)]; `hidden`: {name: value}.
    `confirm` is shown as the button's title (the page has no script to ask)."""
    hid = "".join('<input type=hidden name="%s" value="%s">' % (esc(k), esc(v))
                  for k, v in (hidden or {}).items())
    inputs = "".join('<label>%s<input name="%s" value="%s" autocomplete=off></label>'
                     % (esc(lbl), esc(name), esc(val)) for name, lbl, val in fields)
    return Raw('<form method=post class="%s">%s%s<button type=submit class="%s"%s>%s</button></form>'
               % ("inline" if inline else "row", hid, inputs, "danger" if danger else "act",
                  ' title="%s"' % esc(confirm) if confirm else "", esc(button)))


def posted(body):
    """A form post's fields as {name: value} (the last of a repeated name)."""
    try:
        got = urllib.parse.parse_qsl((body or b"").decode("utf-8", "replace"), keep_blank_values=True)
    except ValueError:
        return {}
    return {k: v.strip() for k, v in got}
