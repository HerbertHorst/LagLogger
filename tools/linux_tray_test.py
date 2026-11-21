#!/usr/bin/env python3
"""Linux: tests the tray icon over D-Bus like a desktop's system tray would (needs PyGObject).

Run inside a D-Bus session:  dbus-run-session -- python3 tools/linux_tray_test.py
A tiny StatusNotifierWatcher stands in for the KDE/GNOME tray; then the menu is clicked via D-Bus.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from gi.repository import Gio, GLib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def ok(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
  <method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
  <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
  <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
  <property name="ProtocolVersion" type="i" access="read"/>
</interface></node>"""
items = []


def watcher():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    iface = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

    def call(conn, sender, path, iface_name, method, params, inv):
        if method == "RegisterStatusNotifierItem":
            items.append(params.unpack()[0])
        inv.return_value(None)

    def prop(conn, sender, path, iface_name, name):
        return {"RegisteredStatusNotifierItems": GLib.Variant("as", items),
                "IsStatusNotifierHostRegistered": GLib.Variant("b", True),
                "ProtocolVersion": GLib.Variant("i", 0)}[name]
    bus.register_object("/StatusNotifierWatcher", iface, call, prop, None)
    Gio.bus_own_name_on_connection(bus, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None)
    GLib.MainLoop().run()


threading.Thread(target=watcher, daemon=True).start()
time.sleep(1)

tmp = tempfile.mkdtemp(prefix="lagscope-tray ")
env = dict(os.environ, LAGSCOPE_DATA=os.path.join(tmp, "data"))
os.makedirs(env["LAGSCOPE_DATA"])
with open(os.path.join(env["LAGSCOPE_DATA"], "config.json"), "w") as f:
    json.dump({"interval": 1.0, "web_port": 18798}, f)
exe = [a for a in sys.argv[1:] if not a.startswith("--")]
base = [sys.executable, exe[0]] if exe else [sys.executable, os.path.join(ROOT, "lagscope.py")]
log = open(os.path.join(tmp, "run.log"), "w")
proc = subprocess.Popen(base + ["run"], env=env, stdout=log, stderr=subprocess.STDOUT)
URL = "http://127.0.0.1:18798/"


def status():
    with urllib.request.urlopen(URL + "api/status", timeout=5) as r:
        return json.loads(r.read())


for _ in range(60):
    time.sleep(0.5)
    if items:
        break
ok(bool(items), f"Tray-Symbol beim Infobereich angemeldet ({items[:1]})")
if items:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    name = items[0]

    def get(path, iface, prop):
        r = bus.call_sync(name, path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, prop)),
                          None, Gio.DBusCallFlags.NONE, 3000, None)
        return r.unpack()[0]

    def call(path, iface, method, args, sig):
        return bus.call_sync(name, path, iface, method, GLib.Variant(sig, args) if sig else None, None,
                             Gio.DBusCallFlags.NONE, 3000, None).unpack()

    title = get("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Title")
    pix = get("/StatusNotifierItem", "org.kde.StatusNotifierItem", "IconPixmap")
    menu = get("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Menu")
    ok(title == "Lagscope", f"Titel: {title}")
    ok(bool(pix) and pix[0][0] == 64 and len(pix[0][2]) == 64 * 64 * 4, f"eigenes Icon ({pix[0][0] if pix else 0} px)")
    rev, layout = call(menu, "com.canonical.dbusmenu", "GetLayout", (0, -1, []), "(iias)")
    labels = [c[1].get("label") for c in layout[2] if c[1].get("label")]
    print("      Menü:", labels, flush=True)
    ok("Lagscope beenden" in labels and "Lag jetzt markieren" in labels, "Menü mit Markieren und Beenden")
    time.sleep(3)
    call(menu, "com.canonical.dbusmenu", "Event", (3, "clicked", GLib.Variant("s", ""), 0), "(isvu)")
    n = 0
    for _ in range(20):
        time.sleep(0.5)
        n = status()["n_markers"]
        if n:
            break
    ok(n == 1, f"Menü 'Lag jetzt markieren' -> Marker ({n})")
    call(menu, "com.canonical.dbusmenu", "Event", (7, "clicked", GLib.Variant("s", ""), 0), "(isvu)")
    try:
        proc.wait(timeout=20)
        ok(True, "Menü 'Lagscope beenden' -> Logger beendet")
    except subprocess.TimeoutExpired:
        ok(False, "Menü 'Lagscope beenden' -> Logger läuft weiter")
if proc.poll() is None:
    proc.kill()
log.close()
text = open(os.path.join(tmp, "run.log"), encoding="utf-8", errors="replace").read()
ok("Traceback" not in text, "keine Fehler im Log")
if FAILS:
    print(text[-2500:])
print("\n" + ("FEHLER: " + str(len(FAILS)) if FAILS else "Alles OK"))
sys.exit(1 if FAILS else 0)
