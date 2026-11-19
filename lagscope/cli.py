"""Command line. Without arguments (double click): start logger if needed and open the browser."""
import argparse
import json
import os
import sys
import time
import urllib.request

from . import __version__, config, osutil


def _alive(url):
    try:
        with urllib.request.urlopen(url + "api/status", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_running(wait=True):
    from . import daemon, server
    if not daemon.is_running():
        try:
            os.remove(server.port_file())
        except OSError:
            pass
        daemon.start_detached()
    if not wait:
        return server.current_url()
    for _ in range(60):
        url = server.current_url()
        if os.path.exists(server.port_file()) and _alive(url):
            return url
        time.sleep(0.25)
    return None


def open_browser(url):
    return osutil.open_url(url)


def cmd_open(args):
    url = ensure_running()
    if not url:
        osutil.error_box("Lagscope konnte nicht starten", f"Details stehen in:\n{config.LOG_FILE}")
        print(f"Logger/Weboberfläche nicht erreichbar – siehe {config.LOG_FILE}")
        return 1
    print(f"Weboberfläche: {url}")
    open_browser(url)
    return 0


def cmd_start(args):
    from . import daemon
    if daemon.is_running():
        print("Logger läuft bereits.")
        return 0
    ensure_running(wait=False)
    print("Logger gestartet.")
    return 0


def cmd_stop(args):
    from . import daemon
    print("Logger gestoppt." if daemon.stop() else "Logger lief nicht.")
    return 0


def cmd_status(args):
    from . import daemon, db, server
    con = db.connect()
    st = server.status(con, config.load())
    st.pop("commands", None)
    print(json.dumps(st, indent=2, ensure_ascii=False, default=str))
    print("Logger läuft" if daemon.is_running() else "Logger läuft NICHT")
    return 0


def cmd_mark(args):
    from .marker import mark
    kind = "good" if args.good else "end" if args.end else "lag"
    mid = mark(kind, args.note, quiet=args.quiet)
    print(f"Marker #{mid} ({kind}) gespeichert.")
    return 0


def cmd_run(args):
    from . import daemon
    return daemon.run(web=not args.no_web)


def cmd_serve(args):
    from . import server
    server.serve_forever(config.load())
    return 0


def cmd_analyze(args):
    from . import analysis, db
    res = analysis.analyze(db.connect(), config.load(), include_auto=not args.no_auto)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"{res['n_incidents']} Vorfälle ({res['n_lag']} Marker, {res['n_auto']} automatisch aus FPS), "
          f"{res['n_good']} 'läuft gut'-Marker\n")
    for s in res["summary"]:
        print(" •", s)
    if res["findings"]:
        print("\nAuffällige Messwerte:")
        for f in res["findings"][:10]:
            print(f"  {f['label']:<32} {'↑' if f['direction'] == 'up' else '↓'} {f['hits']}/{f['n']}  "
                  f"{f['base']} → {f['bad']} {f['unit']}")
    if res["processes"]:
        print("\nProzesse:")
        for p in res["processes"][:10]:
            print(f"  {p['name']:<24} mehr CPU: {p['up']}  neu: {p['new']}  ({p['cpu_base']} → {p['cpu_bad']} %)")
    return 0


def cmd_selftest(args):
    from . import selftest
    return selftest.run(as_json=args.json, seconds=args.seconds)


def cmd_import(args):
    from . import db, mangohud
    con = db.connect()
    for p in args.files:
        mangohud.import_file(con, p)
    return 0


def main(argv=None):
    osutil.attach_console()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="lagscope", description="Findet heraus, warum Spiele plötzlich ruckeln.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("open", help="Logger starten (falls nötig) und Auswertung im Browser öffnen [Standard]")
    sub.add_parser("start", help="Logger im Hintergrund starten")
    sub.add_parser("stop", help="Logger stoppen")
    sub.add_parser("status", help="Status anzeigen")
    p = sub.add_parser("mark", help="Zeitpunkt markieren (für Tastenkürzel)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--good", action="store_true", help="'läuft gut' statt 'Lag'")
    g.add_argument("--end", action="store_true", help="'Lag vorbei'")
    p.add_argument("--quiet", action="store_true", help="keine Desktop-Benachrichtigung")
    p.add_argument("note", nargs="?", help="optionale Notiz")
    p = sub.add_parser("run", help="Logger im Vordergrund laufen lassen")
    p.add_argument("--no-web", action="store_true")
    sub.add_parser("serve", help="Nur die Weboberfläche starten")
    p = sub.add_parser("analyze", help="Analyse im Terminal ausgeben")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-auto", action="store_true", help="FPS-Einbrüche aus MangoHud nicht automatisch einbeziehen")
    p = sub.add_parser("selftest", help="Prüfen, welche Messwerte auf diesem PC ankommen")
    p.add_argument("--json", action="store_true")
    p.add_argument("--seconds", type=float, default=4.0)
    p = sub.add_parser("import", help="MangoHud-CSV-Dateien importieren")
    p.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    fn = {"open": cmd_open, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "mark": cmd_mark,
          "run": cmd_run, "serve": cmd_serve, "analyze": cmd_analyze, "import": cmd_import,
          "selftest": cmd_selftest}.get(args.cmd or "open")
    return fn(args)
