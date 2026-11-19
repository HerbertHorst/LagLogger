#!/usr/bin/env python3
"""End-to-end test, runs the same on Linux and Windows (also used by GitHub Actions).

    python tools/smoke_test.py            # tests the source
    python tools/smoke_test.py --exe dist/lagscope.exe   # tests a built program (zipapp or Windows EXE)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def ok(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def main():
    exe = None
    if "--exe" in sys.argv:
        exe = os.path.abspath(sys.argv[sys.argv.index("--exe") + 1])
    base = [exe] if exe and exe.lower().endswith(".exe") else [sys.executable, exe] if exe else \
        [sys.executable, os.path.join(ROOT, "lagscope.py")]
    tmp = tempfile.mkdtemp(prefix="lagscope-test ")
    env = dict(os.environ, LAGSCOPE_DATA=os.path.join(tmp, "data"), PYTHONIOENCODING="utf-8")
    print(f"Python {sys.version.split()[0]} on {sys.platform}; testing {' '.join(base)}", flush=True)

    def run(*args, timeout=120):
        return subprocess.run(base + list(args), env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)

    # ---- 1. Analyse auf Demo-Daten (nur mit Quellcode)
    if not exe:
        print("Demo-Daten + Analyse", flush=True)
        demo_env = dict(env, LAGSCOPE_DATA=os.path.join(tmp, "demo"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "demo_data.py")], env=demo_env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        ok(r.returncode == 0, "Demo-Daten erzeugt" + ("" if r.returncode == 0 else ": " + r.stderr[-500:]))
        r = subprocess.run(base + ["analyze", "--json"], env=demo_env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        ok(r.returncode == 0, "Analyse läuft" + ("" if r.returncode == 0 else ": " + r.stderr[-800:]))
        if r.returncode == 0:
            a = json.loads(r.stdout)
            keys = {f["key"] for f in a["findings"]}
            alerts = {x["key"] for x in a["alerts"]}
            ok(a["n_lag"] == 4, f"4 Lag-Marker gefunden ({a['n_lag']})")
            ok("vram_pct" in alerts, "Analyse erkennt volles VRAM")
            ok("shader_cpu" in keys, "Analyse erkennt Shader-Kompilierung")
            ok(any(p["name"] == "fossilize_replay" for p in a["processes"]), "Analyse erkennt Prozess fossilize_replay")
        # Nur die Lag-Taste (keine "läuft gut"-Marker): FPS liefern Vergleich und Lag-Dauer
        import sqlite3
        c = sqlite3.connect(os.path.join(demo_env["LAGSCOPE_DATA"], "lagscope.db"))
        c.execute("DELETE FROM markers WHERE kind != 'lag'")
        c.commit()
        c.close()
        r = subprocess.run(base + ["analyze", "--json"], env=demo_env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        if r.returncode == 0:
            a = json.loads(r.stdout)
            ok(a["n_good"] == 0 and all(i["by_fps"] for i in a["incidents"]),
               "nur Lag-Marker: Zeitfenster aus den FPS")
            ok(all("normalen FPS" in (i["baseline"] or "") for i in a["incidents"]),
               "nur Lag-Marker: Vergleich mit Momenten normaler FPS")
            ok({"vram_pct", "shader_cpu"} <= {x["key"] for x in a["alerts"]} and
               any(p["name"] == "fossilize_replay" and p["new"] >= 3 for p in a["processes"]),
               "nur Lag-Marker: VRAM und Shader-Kompilierung trotzdem erkannt")
        else:
            ok(False, "Analyse ohne 'läuft gut'-Marker: " + r.stderr[-500:])

    # ---- 2. Selbsttest
    print("Selbsttest", flush=True)
    r = run("selftest", "--json", "--seconds", "2")
    ok(r.returncode == 0, "selftest ohne Fehler" + ("" if r.returncode == 0 else ": " + (r.stdout + r.stderr)[-1500:]))
    try:
        st = json.loads(r.stdout)
        ok("cpu_total" in st["metrics"] and "mem_avail" in st["metrics"], "CPU- und RAM-Werte werden gelesen")
        ok(st["sample_ms"] < 500, f"eine Messung dauert {st['sample_ms']} ms")
        print("      fehlend: " + ", ".join(st["missing"]) if st["missing"] else "      nichts fehlt", flush=True)
        print("      Werte: " + ", ".join(sorted(st["metrics"])), flush=True)
    except (ValueError, KeyError) as e:
        ok(False, f"selftest-JSON lesbar ({e!r}): {r.stdout[-500:]}")

    # ---- 3. Echter Logger-Lauf mit Weboberfläche
    print("Logger + Weboberfläche", flush=True)
    os.makedirs(env["LAGSCOPE_DATA"], exist_ok=True)
    with open(os.path.join(env["LAGSCOPE_DATA"], "config.json"), "w") as f:
        json.dump({"interval": 1.0, "web_port": 18765, "game_patterns": ["overwatch", "lagscope-fake-game"]}, f)
    log = open(os.path.join(tmp, "run.log"), "w")
    proc = subprocess.Popen(base + ["run"], env=env, stdout=log, stderr=subprocess.STDOUT)
    url = None
    for _ in range(80):
        time.sleep(0.5)
        try:
            with open(os.path.join(env["LAGSCOPE_DATA"], "port")) as f:
                url = f.read().strip()
            urllib.request.urlopen(url + "api/status", timeout=2).read()
            break
        except Exception:
            url = None
    ok(url is not None, "Weboberfläche erreichbar")
    if url:
        time.sleep(18)  # mindestens einen Zwischenstand speichern lassen
        r = run("mark", "--quiet", "Test-Marker")
        ok(r.returncode == 0 and "gespeichert" in r.stdout, "Marker per Befehl gesetzt")

        def get(path):
            with urllib.request.urlopen(url + path, timeout=30) as resp:
                return resp.read()
        st = json.loads(get("api/status"))
        ok(st["running"], "Status: Logger läuft")
        ok(st["last_sample"] and time.time() - st["last_sample"] < 30, "aktuelle Messwerte gespeichert")
        ok(st["n_markers"] == 1, "Marker in der Datenbank")
        ok(st["self_cpu"] is not None and st["self_cpu"] < 10, f"Eigenverbrauch {st['self_cpu']} % CPU")
        print(f"      Erkannt: {st['capabilities']}", flush=True)
        print(f"      Eigenverbrauch: {st['self_cpu']} % CPU, {st['self_rss']} MB", flush=True)
        info = json.loads(get("api/info"))
        keys = [m["key"] for m in info["catalogue"]["metrics"]]
        now = time.time()
        ser = json.loads(get(f"api/series?from={now - 120}&to={now}&keys={','.join(keys)}&points=500"))
        ok(len(ser["t"]) >= 5 and "cpu_total" in ser["series"], f"Zeitreihe ({len(ser['t'])} Punkte)")
        ok(b"Lagscope" in get(""), "Startseite")
        ok(len(get("app.js")) > 1000 and len(get("uPlot.min.js")) > 1000, "Skripte werden ausgeliefert")
        ok(len(json.loads(get(f"api/procs?ts={now - 5}"))["procs"]) > 0, "Prozessliste")
        an = json.loads(get("api/analysis?refresh=1"))
        for _ in range(60):
            if an.get("result") or an.get("error"):
                break
            time.sleep(0.5)
            an = json.loads(get("api/analysis"))
        ok(an.get("result") is not None and not an.get("error"), "Analyse über die Weboberfläche")
        r = run("stop")
        ok(r.returncode == 0, "lagscope stop")
    try:
        proc.wait(timeout=20)
        ok(True, "Logger hat sich beendet")
    except subprocess.TimeoutExpired:
        proc.kill()
        ok(False, "Logger hat sich nicht beendet")
    log.close()
    with open(os.path.join(tmp, "run.log"), encoding="utf-8", errors="replace") as f:
        text = f.read()
    ok("Traceback" not in text, "keine Fehler im Logger-Log")
    ok("Beendet." in text, "Logger hat sauber gespeichert und beendet")
    if FAILS or "-v" in sys.argv:
        print("---- Logger-Log ----\n" + text[-3000:])

    # ---- 4. Lag-Zeitfenster aus FPS (Einbruch beginnt vor dem Tastendruck und endet später)
    if not exe:
        sys.path.insert(0, ROOT)
        from lagscope import analysis
        t0 = 1_000_000
        rows = [(t0 + i, 150.0 if (i < 1200 or i >= 1500) else 40.0, 10, 7) for i in range(2400)]
        sf = analysis.SessionFps(rows, t0)
        span = sf.lag_span(t0 + 1290, t0 + 60, t0 + 2400)   # Taste 90 s nach Beginn des Einbruchs gedrückt
        ok(span is not None and abs(span[0] - (t0 + 1200)) <= 10 and abs(span[1] - (t0 + 1500)) <= 15,
           f"Lag-Beginn/-Ende aus FPS erkannt ({span and (span[0] - t0, span[1] - t0)} statt 1200/1500)")
        ok(sf.lag_span(t0 + 600, t0 + 60, t0 + 2400) is None, "Marker bei normalen FPS bleibt beim Standardfenster")
        # Lag bis Sitzungsende (60 % der Sitzung): "normal" darf nicht der Lag-Wert sein
        rows = [(t0 + i, 150.0 if i < 1000 else 40.0, 10, 7) for i in range(2500)]
        sf = analysis.SessionFps(rows, t0)
        ok(sf.ref >= 140, f"Bezug 'normale FPS' trotz langem Lag richtig ({sf.ref})")

    # ---- 5. MangoHud-Import
    if not exe:
        sys.path.insert(0, ROOT)
        from lagscope import mangohud
        p = os.path.join(tmp, "Overwatch_2026-01-02_20-00-00.csv")
        rows = ["os,cpu,gpu", "x,y,z", "--------------------FRAME METRICS--------------------",
                "fps,frametime,cpu_load,gpu_load,elapsed"]
        el = 0
        for i in range(2000):
            ft = 7.0 if i < 1500 else 25.0
            el += int(ft * 1e6)
            rows.append(f"{1000 / ft:.1f},{ft},40,90,{el}")
        with open(p, "w") as f:
            f.write("\n".join(rows))
        data = mangohud.parse(p)
        ok(len(data) > 20 and data[0][1] > 120 and data[-1][1] < 50, f"MangoHud-CSV gelesen ({len(data)} s)")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FEHLER: ' + str(len(FAILS)) if FAILS else 'Alles OK'}", flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
