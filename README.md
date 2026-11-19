# Lagscope

Findet heraus, **warum ein Spiel unter Linux plötzlich ruckelt** – z. B. wenn Overwatch statt 100–160 FPS
auf einmal nur noch 20–60 FPS schafft und nur ein Neustart hilft.

Lagscope läuft unauffällig im Hintergrund und schreibt alle paar Sekunden mit, was der PC gerade macht
(CPU, GPU, VRAM, RAM, Temperaturen, Takt, Engpässe, Festplatte, Netzwerk, Prozesse, Kernel-Meldungen, Fenster im
Vordergrund …). Sobald es ruckelt, drückst du ein **Tastenkürzel** – das setzt einen Marker. Später zeigt die
**Auswertung im Browser**, was die Lag-Momente gemeinsam haben – über mehrere Tage hinweg, automatisch.

![Analyse](docs/analyse.png)

![Verlauf mit Lag-Marker](docs/verlauf.png)

- **Portabel:** eine einzige Datei, irgendwohin kopieren, doppelklicken. Keine Installation, keine Pakete.
- **Sparsam:** ca. 0,1–0,5 % eines CPU-Kerns und ~30 MB RAM, niedrige CPU- und IO-Priorität. Der eigene
  Verbrauch wird mitgemessen und in der Oberfläche angezeigt.
- **Flüssige Auswertung:** der Browser bekommt nie Rohdaten, sondern immer nur ≤1500 vorberechnete Punkte pro
  Linie für den sichtbaren Zeitraum – auch bei Wochen an Daten.
- **Nur lokal:** die Weboberfläche ist nur auf diesem PC erreichbar (127.0.0.1), es wird nichts ins Internet
  geschickt.
- **Nur Linux** (liest `/proc`, `/sys` und das Kernel-Log). Python 3.9 oder neuer ist auf praktisch jeder
  Distribution schon vorhanden.

## Schnellstart

1. Den Ordner [`dist/`](dist) herunterladen (bzw. das ganze Repo als ZIP) und irgendwohin kopieren,
   z. B. nach `~/Programme/lagscope/`. Neben der Datei entsteht später der Datenordner `lagscope-data/`.
2. **Doppelklick auf `Lagscope.desktop`** (beim ersten Mal ggf. „Vertrauen“ / „Ausführen erlauben“ bestätigen).
   Alternativ im Terminal: `python3 lagscope`
   → Der Logger startet im Hintergrund und die Auswertung öffnet sich im Browser.
3. Im Tab **Einrichtung**:
   - **Autostart aktivieren** – damit nach jedem Neustart automatisch mitgeschrieben wird.
   - **Menüeinträge anlegen** und dann ein **Tastenkürzel** auf „Lagscope: Lag markieren“ legen
     (KDE: Systemeinstellungen → Tastatur → Kurzbefehle → Neu → Anwendung;
     GNOME: Einstellungen → Tastatur → Eigene Tastenkombinationen, Befehl aus der Einrichtung kopieren).
   - optional **MangoHud** für echte FPS-Werte (siehe unten).
4. Spielen. Wenn es ruckelt: **Tastenkürzel drücken**. Wenn es wieder gut läuft (oder ab und zu zwischendurch):
   „Läuft gut“ markieren – das verbessert den Vergleich.
5. Nach ein paar Tagen / Markern: Tab **Analyse** öffnen.

Die Auswertung kann man jederzeit wieder per Doppelklick öffnen (der Logger läuft weiter, auch wenn der Browser
zu ist).

## Was die Marker bedeuten

| Marker | Bedeutung |
|---|---|
| **Lag** | „Jetzt laggt es.“ Ausgewertet werden die 90 s davor bis 60 s danach (der Lag hat meist schon kurz vorher angefangen). Beim Drücken werden zusätzlich das Fenster im Vordergrund und die komplette Prozessliste gespeichert. |
| **Läuft gut** | „Jetzt ist alles normal.“ Dient als Vergleich. |
| **Lag vorbei** | Beendet das Lag-Zeitfenster vorzeitig (z. B. nach Neustart des Spiels). |

Marker kann man auch nachträglich im Verlauf setzen (in ein Diagramm klicken → „Hier war Lag“), mit Notizen
versehen oder löschen.

## FPS mit MangoHud (empfohlen)

Lagscope kann die FPS nicht selbst messen. [MangoHud](https://github.com/flightlessmango/MangoHud) kann sie
aber in eine Datei schreiben, die Lagscope automatisch einliest. Dann werden FPS-Einbrüche **auch ohne
Tastendruck** erkannt und es wird berechnet, welche Messwerte am stärksten mit den FPS zusammenhängen.

1. MangoHud über die Paketverwaltung installieren (Paket `mangohud`).
2. In Steam: Rechtsklick auf das Spiel → Eigenschaften → Startoptionen. Den fertigen Befehl (mit dem richtigen
   Ordner) zeigt Lagscope im Tab **Einrichtung** zum Kopieren an. Er sieht so aus:
   ```
   MANGOHUD=1 MANGOHUD_CONFIG="autostart_log=1,output_folder=/pfad/zu/lagscope-data/mangohud" %command%
   ```

Je nach MangoHud-Version wird die Datei erst geschrieben, wenn das Spiel beendet wird.

## Wie die Analyse funktioniert

Für jeden Vorfall (Marker oder automatisch erkannter FPS-Einbruch) wird das Zeitfenster rund um den Lag mit
**normalem Spielen** verglichen – bevorzugt mit derselben Spielsitzung vor dem Lag, sonst mit „Läuft gut“-Markern
oder Sitzungen ganz ohne Lag. Gemeldet wird nur, was bei **den meisten** Vorfällen auffällt:

- **Messwerte**, die bei Lags deutlich höher/niedriger sind (robuster Vergleich mit Median, damit einzelne
  Ausreißer nichts verfälschen) – jeweils mit Erklärung, was das bedeutet und was man tun kann.
- **Warnsignale** wie fast volles VRAM, Temperaturen im Drosselbereich, Swap, ein Kern auf 100 %.
- **Prozesse**, die bei Lags mehr CPU brauchen oder neu auftauchen – und Prozesse, die in Sitzungen mit Lag
  laufen, in Sitzungen ohne Lag aber kaum.
- **Kernel-Meldungen** rund um die Lags (z. B. Split-Lock-Warnungen, GPU-Resets).
- **Zeitlicher Zusammenhang:** wie viele Minuten nach Spielstart, wie lange der PC schon lief.
- **FPS-Zusammenhang** (mit MangoHud): welche Werte mit sinkenden FPS einhergehen.

Typische Ursachen, die Lagscope so aufdecken kann:

| Ursache | Woran man es erkennt |
|---|---|
| VRAM läuft voll (nach einigen Runden) | VRAM ≥ 92 %, GTT steigt, GPU-Last und -Takt sinken |
| Steam kompiliert Shader im Hintergrund | Prozess `fossilize_replay`, ein Kern auf 100 %, Disk-Lesen |
| CPU/GPU drosselt (Temperatur/Energieprofil) | Takt sinkt, Temperatur hoch, Throttle-Zähler, Wechsel des Energieprofils |
| Arbeitsspeicher knapp / Speicher-Fragmentierung | Speicher-Druck (PSI), Swap, Compaction-Stalls, wird mit Uptime schlimmer |
| Hintergrundprogramme | Indexer (baloo, tracker), Updates, Browser, Discord, OBS … |
| Kernel bremst das Spiel | Split-Lock-Meldungen im Kernel-Log |

## Was alles gemessen wird

CPU (gesamt, stärkster Kern, IO-Wait, laufbereite/blockierte Threads), CPU-Takt und -Temperatur,
Throttle-Ereignisse, Governor/Energieprofil, GPU (AMD und Intel über sysfs, NVIDIA über `nvidia-smi`): Last,
Takt, VRAM, GTT, Temperatur, Hotspot, Leistung, Drossel-Gründe, Power-Profil; RAM, Swap, Dirty-Pages, Huge Pages;
Pressure Stall Information (CPU/Speicher/IO); Page Faults, Swap-Aktivität, Compaction, Direct Reclaim;
Datenträger und Netzwerk; Spielprozess (CPU, RAM, Threads), wineserver, Shader-Kompilierung; Prozessliste
(alle 6 s die Top-Prozesse); Fenster im Vordergrund; Prozess-Starts/-Enden; Kernel-Log; Systemstarts.

Hilfsprogramme, die Lagscope nutzt, wenn sie vorhanden sind (alles optional): `xprop`/`xdotool`/`kdotool`
(Fenster im Vordergrund), `notify-send` (Bestätigung beim Markieren), `nvidia-smi`, `journalctl`, `mangohud`.

## Speicherplatz

Etwa 5–15 MB pro Tag im Datenordner `lagscope-data/`. Ältere Daten als 14 Tage werden automatisch gelöscht
(Marker bleiben länger erhalten).

## Befehle

Ohne Argument (Doppelklick) entspricht `open`.

```
python3 lagscope            # Logger starten (falls nötig) + Browser öffnen
python3 lagscope start      # nur Logger im Hintergrund starten
python3 lagscope stop       # Logger beenden
python3 lagscope status     # Status anzeigen
python3 lagscope mark       # Lag markieren        (für das Tastenkürzel)
python3 lagscope mark --good  # "läuft gut" markieren
python3 lagscope mark --end   # "Lag vorbei" markieren
python3 lagscope analyze    # Analyse im Terminal
python3 lagscope import datei.csv   # MangoHud-Log manuell importieren
```

## Einstellungen

`lagscope-data/config.json` (wird beim ersten Start angelegt; nach Änderungen Logger neu starten):

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `interval` | 2.0 | Sekunden zwischen zwei Messungen |
| `game_patterns` | `["overwatch"]` | woran das Spiel erkannt wird (Teil des Prozessnamens) |
| `retention_days` | 14 | wie lange Messdaten aufgehoben werden |
| `web_port` | 8765 | Port der Weboberfläche (ist er belegt, wird der nächste freie genommen) |
| `mangohud_dir` | `""` | Ordner der MangoHud-Logs (leer = `lagscope-data/mangohud`) |
| `lag_before` / `lag_after` | 90 / 60 | Sekunden vor/nach einem Lag-Marker, die ausgewertet werden |
| `auto_fps_ratio` | 0.6 | FPS-Einbruch = FPS unter 60 % des Sitzungs-Medians … |
| `auto_min_seconds` | 10 | … für mindestens so viele Sekunden |

## Entfernen

Im Tab Einrichtung Autostart und Menüeinträge entfernen (oder die Dateien `~/.config/autostart/lagscope.desktop`
und `~/.local/share/applications/lagscope-*.desktop` löschen), Logger beenden, Ordner löschen.

## Entwicklung

```
python3 lagscope.py              # direkt aus dem Quellordner starten
python3 build.py                 # baut die portable Einzeldatei dist/lagscope
LAGSCOPE_DATA=/tmp/demo python3 tools/demo_data.py   # Demo-Daten erzeugen …
LAGSCOPE_DATA=/tmp/demo python3 lagscope.py serve    # … und ansehen
```

Reines Python (nur Standardbibliothek). Diagramme mit [uPlot](https://github.com/leeoniya/uPlot) (MIT, liegt bei).
