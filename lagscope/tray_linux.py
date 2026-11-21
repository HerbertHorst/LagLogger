"""Linux: icon in the system tray (StatusNotifierItem over D-Bus – what KDE Plasma shows natively; GNOME with
the AppIndicator extension, which Bazzite/Ubuntu ship).

Uses PyGObject (Gio) from the system Python – preinstalled on Bazzite, Fedora, Ubuntu and most desktops.
Without it, or without a tray host, Lagscope simply runs without an icon.

Left click: open the report. Right click: open report, mark lag / good / lag over, quit.
"""
import os
import threading
import zlib

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ia{s})" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  </interface>
</node>"""

# id -> (Beschriftung, Aktion); None = Trennlinie
MENU = [
    (1, "Auswertung öffnen", "open"),
    (2, None, None),
    (3, "Lag jetzt markieren", "lag"),
    (4, "Läuft gut markieren", "good"),
    (5, "Lag vorbei markieren", "end"),
    (6, None, None),
    (7, "Lagscope beenden", "quit"),
]


def png_to_argb(data):
    """Decode the (filter-0, RGBA, 8 bit) PNG Lagscope ships as its icon into SNI IconPixmap data."""
    import struct
    pos, w, h, idat = 8, 0, 0, b""
    while pos < len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + w * 4
    out = bytearray()
    for y in range(h):
        row = raw[y * stride + 1:(y + 1) * stride]  # Filterbyte überspringen (immer 0 bei unserem Icon)
        for x in range(w):
            r, g, b, a = row[x * 4:x * 4 + 4]
            out += bytes((a, r, g, b))  # ARGB32, Netzwerk-Byte-Reihenfolge
    return w, h, bytes(out)


class Tray:
    """callbacks: open(), mark(kind), quit(). Raises ImportError/RuntimeError if no tray is possible."""

    def __init__(self, icon_png, on_open, on_mark, on_quit):
        from gi.repository import Gio, GLib  # noqa: F401  (ImportError -> kein Tray)
        self.Gio, self.GLib = Gio, GLib
        self.on_open, self.on_mark, self.on_quit = on_open, on_mark, on_quit
        self.tip = "Lagscope – zeichnet auf"
        try:
            self.pixmap = [png_to_argb(icon_png)] if icon_png else []
        except Exception:
            self.pixmap = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.loop = GLib.MainLoop()
        self.registered = threading.Event()
        self.error = None
        threading.Thread(target=self._run, daemon=True, name="tray").start()
        self.registered.wait(5)
        if self.error:
            raise RuntimeError(self.error)

    # ------------------------------------------------------------------ D-Bus
    def _run(self):
        Gio, GLib = self.Gio, self.GLib
        try:
            item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self.bus.register_object("/StatusNotifierItem", item, self._item_call, self._item_prop, None)
            self.bus.register_object("/MenuBar", menu, self._menu_call, self._menu_prop, None)
            Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                               "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                               GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception as e:
            self.error = f"kein Infobereich (StatusNotifierWatcher) gefunden: {e}"
            self.registered.set()
            return
        self.registered.set()
        self.loop.run()

    def _item_prop(self, conn, sender, path, iface, prop):
        V = self.GLib.Variant
        values = {
            "Category": V("s", "ApplicationStatus"), "Id": V("s", "lagscope"), "Title": V("s", "Lagscope"),
            "Status": V("s", "Active"), "WindowId": V("i", 0), "IconName": V("s", "utilities-system-monitor"),
            "IconPixmap": V("a(iiay)", self.pixmap), "OverlayIconName": V("s", ""), "AttentionIconName": V("s", ""),
            "ToolTip": V("(sa(iiay)ss)", ("", [], "Lagscope", self.tip)),
            "ItemIsMenu": V("b", False), "Menu": V("o", "/MenuBar"),
        }
        return values.get(prop)

    def _item_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self._do("open")
        invocation.return_value(None)

    def _props(self, mid, label):
        V = self.GLib.Variant
        if label is None:
            return {"type": V("s", "separator")}
        p = {"label": V("s", label), "enabled": V("b", True), "visible": V("b", True)}
        if mid == 1:
            p["toggle-type"] = V("s", "")
        return p

    def _layout(self):
        V = self.GLib.Variant
        children = [V("(ia{sv}av)", (mid, self._props(mid, label), [])) for mid, label, _ in MENU]
        return (0, {"children-display": V("s", "submenu")}, children)

    def _menu_prop(self, conn, sender, path, iface, prop):
        V = self.GLib.Variant
        return {"Version": V("u", 3), "TextDirection": V("s", "ltr"), "Status": V("s", "normal"),
                "IconThemePath": V("as", [])}.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        V = self.GLib.Variant
        if method == "GetLayout":
            invocation.return_value(V("(u(ia{sv}av))", (1, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            res = [(mid, self._props(mid, label)) for mid, label, _ in MENU if not ids or mid in ids]
            invocation.return_value(V("(a(ia{sv}))", (res,)))
        elif method == "GetProperty":
            mid, name = params.unpack()
            props = next((self._props(m, label) for m, label, _ in MENU if m == mid), {})
            invocation.return_value(V("(v)", (props.get(name, V("s", "")),)))
        elif method == "Event":
            mid, event = params.unpack()[0], params.unpack()[1]
            if event == "clicked":
                self._click(mid)
            invocation.return_value(None)
        elif method == "EventGroup":
            for mid, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    self._click(mid)
            invocation.return_value(V("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(V("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(V("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _click(self, mid):
        action = next((a for m, _, a in MENU if m == mid), None)
        if action:
            self._do(action)

    def _do(self, action):
        # nicht im D-Bus-Thread arbeiten (Browser starten kann dauern)
        def run():
            try:
                if action == "open":
                    self.on_open()
                elif action == "quit":
                    self.on_quit()
                else:
                    self.on_mark(action)
            except Exception as e:
                print(f"Tray: {e!r}", flush=True)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ from other threads
    def set_tip(self, text):
        if text == self.tip:
            return
        self.tip = text
        try:
            self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", None)
        except Exception:
            pass

    def close(self):
        try:
            self.loop.quit()
        except Exception:
            pass
