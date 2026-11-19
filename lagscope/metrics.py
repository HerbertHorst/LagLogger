"""Catalogue of all recorded metrics: label, unit, chart group, and the smallest change that matters.

`floor` keeps the analysis from treating tiny absolute changes on very flat metrics as huge effects.
`hint_up` / `hint_down` explain what a consistent rise / drop during lags usually means.
"""

GROUPS = [
    ("fps", "FPS (MangoHud)"),
    ("cpu", "CPU-Auslastung"),
    ("cpufreq", "CPU-Takt & Temperatur"),
    ("gpu", "GPU"),
    ("vram", "VRAM"),
    ("mem", "Arbeitsspeicher"),
    ("pressure", "Engpässe (PSI)"),
    ("vm", "Speicherverwaltung"),
    ("io", "Datenträger & Netzwerk"),
    ("game", "Spielprozess"),
    ("misc", "Sonstiges"),
]

M = {}


def m(key, label, unit, group, floor, hint_up=None, hint_down=None, chart=True):
    M[key] = dict(key=key, label=label, unit=unit, group=group, floor=floor,
                  hint_up=hint_up, hint_down=hint_down, chart=chart)


HINT_RAM = ("Der Arbeitsspeicher wird knapp: Linux lagert aus oder muss Speicher freiräumen. "
            "Browser/Discord schließen, Swap/zram prüfen, ggf. mehr RAM.")
HINT_COMPACT = ("Der Kernel räumt Speicher um (Compaction für Transparent Huge Pages). Das kann Spiele "
                "spürbar ruckeln lassen und wird mit längerer Laufzeit schlimmer (passt zu 'Neustart hilft'). "
                "Test: `echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/defrag` und "
                "`echo 0 | sudo tee /proc/sys/vm/compaction_proactiveness`.")

m("fps", "FPS", "fps", "fps", 5, None, "FPS sind gefallen (Messwert von MangoHud).")
m("ft_max", "Längster Frame", "ms", "fps", 5, "Einzelne sehr lange Frames (Ruckler/Stotterer).")

m("cpu_total", "CPU gesamt", "%", "cpu", 5,
  "Die CPU ist insgesamt stärker ausgelastet. Wenn der Spielprozess nicht mehr CPU braucht, frisst etwas anderes "
  "Leistung – siehe Prozesse.")
m("cpu_max_core", "Stärkster Kern", "%", "cpu", 5,
  "Ein einzelner CPU-Kern läuft am Anschlag – typisch für einen Hintergrundprozess, der sich mit dem Spiel um "
  "einen Kern streitet.")
m("cpu_iowait", "IO-Wait", "%", "cpu", 2, "Die CPU wartet auf die Festplatte.")
m("cpu_steal", "Steal", "%", "cpu", 1, chart=False)
m("procs_running", "Laufbereite Threads", "", "cpu", 3,
  "Mehr Threads wollen gleichzeitig rechnen als sonst – CPU-Konkurrenz.")
m("procs_blocked", "Blockierte Threads (D)", "", "cpu", 1, "Prozesse hängen in Festplatten-/Treiber-Operationen.")
m("ctxt_s", "Kontextwechsel/s", "/s", "cpu", 5000, chart=False)
m("load1", "Load (1 min)", "", "cpu", 1, chart=False)

m("cpu_freq_avg", "CPU-Takt Ø", "MHz", "cpufreq", 150, None,
  "Die CPU taktet niedriger als sonst – Energiesparmodus, Temperatur- oder Strom-Limit. Governor/Energieprofil "
  "prüfen (z. B. 'performance' / power-profiles-daemon) und Kühlung kontrollieren.")
m("cpu_freq_max", "CPU-Takt max", "MHz", "cpufreq", 150, None,
  "Selbst der schnellste Kern taktet niedriger – deutet auf Temperatur-/Power-Limit oder Energiesparprofil.")
m("cpu_temp", "CPU-Temperatur", "°C", "cpufreq", 3,
  "Die CPU wird wärmer. Bei >90 °C drosselt sie – Lüfter/Kühler/Wärmeleitpaste prüfen.")
m("cpu_throttle_s", "Throttle-Ereignisse/s", "/s", "cpufreq", 0.5,
  "Die CPU drosselt sich wegen Temperatur (Kernel-Zähler thermal_throttle).")

m("gpu_busy", "GPU-Last", "%", "gpu", 8, None,
  "Die GPU hat weniger zu tun – das Spiel wartet auf etwas anderes (CPU, Speicher, Shader-Kompilierung).")
m("gpu_clock", "GPU-Takt", "MHz", "gpu", 150, None,
  "Die GPU taktet herunter. AMD: power_dpm_force_performance_level / CoreCtrl prüfen; NVIDIA: PowerMizer "
  "'Prefer Maximum Performance'. Kann auch Temperatur- oder Power-Limit sein.")
m("gpu_mclk", "VRAM-Takt", "MHz", "gpu", 100, None, "Der Grafikspeicher taktet herunter (Energiesparzustand).")
m("gpu_temp", "GPU-Temperatur", "°C", "gpu", 3, "Die Grafikkarte wird wärmer – Kühlung/Lüfterkurve prüfen.")
m("gpu_temp_hot", "GPU Hotspot", "°C", "gpu", 3, "Der GPU-Hotspot steigt – nahe ~100 °C drosselt die Karte.")
m("gpu_power", "GPU-Leistung", "W", "gpu", 10, None, "Die GPU zieht weniger Strom – sie arbeitet weniger.")
m("gpu_throttle", "GPU-Drossel-Grund (NVIDIA)", "", "gpu", 0.5,
  "NVIDIA meldet einen aktiven Drossel-Grund (Temperatur/Power/Takt-Limit).")
m("gpu_pstate", "P-State (NVIDIA)", "", "gpu", 0.5, "Die NVIDIA-Karte ist in einem stromsparenden P-State.")

m("vram_used", "VRAM belegt", "MB", "vram", 300,
  "Der Grafikspeicher füllt sich. Ist er voll, wird über PCIe ausgelagert und die FPS brechen ein – oft durch "
  "zu hohe Texturqualität oder ein Speicherleck; andere GPU-Programme (Browser, Discord, OBS) schließen.")
m("vram_pct", "VRAM belegt (%)", "%", "vram", 4,
  "Der Grafikspeicher ist fast voll – siehe 'VRAM belegt'.")
m("gtt_used", "GTT (ausgelagert in RAM)", "MB", "vram", 200,
  "Die GPU lagert Daten in den normalen Arbeitsspeicher aus (GTT). Typisch, wenn das VRAM voll ist.")

m("mem_used", "RAM belegt", "MB", "mem", 500, "Mehr Arbeitsspeicher belegt.")
m("mem_avail", "RAM frei", "MB", "mem", 500, None, HINT_RAM)
m("swap_used", "Swap belegt", "MB", "mem", 200, HINT_RAM)
m("mem_dirty", "Dirty-Pages", "MB", "mem", 100, "Es wird gerade viel auf die Platte geschrieben.", chart=False)
m("anon_huge", "Transparent Huge Pages", "MB", "mem", 200, chart=False)

m("psi_cpu", "CPU-Druck", "%", "pressure", 3, "Prozesse müssen auf CPU-Zeit warten (CPU ist der Engpass).")
m("psi_mem", "Speicher-Druck", "%", "pressure", 1, HINT_RAM)
m("psi_mem_full", "Speicher-Druck (voll)", "%", "pressure", 0.5, HINT_RAM)
m("psi_io", "IO-Druck", "%", "pressure", 2,
  "Prozesse warten auf die Festplatte (Updates, Indexer wie baloo/tracker, Steam-Downloads).")

m("pgmajfault_s", "Major Page Faults/s", "/s", "vm", 20, HINT_RAM)
m("pswpin_s", "Swap-in/s", "Seiten/s", "vm", 10, HINT_RAM)
m("pswpout_s", "Swap-out/s", "Seiten/s", "vm", 10, HINT_RAM)
m("compact_stall_s", "Compaction-Stalls/s", "/s", "vm", 0.5, HINT_COMPACT)
m("pgscan_direct_s", "Direct Reclaim/s", "Seiten/s", "vm", 50, HINT_RAM)
m("thp_fallback_s", "THP-Fallbacks/s", "/s", "vm", 1, HINT_COMPACT)

m("disk_read", "Disk lesen", "MB/s", "io", 5, "Es wird viel von der Platte gelesen (Shader-Cache, Updates, Indexer).")
m("disk_write", "Disk schreiben", "MB/s", "io", 5, "Es wird viel auf die Platte geschrieben (Shader-Cache, Updates, Downloads).")
m("disk_busy", "Disk ausgelastet", "%", "io", 5, "Die Festplatte ist stark beschäftigt.")
m("net_rx", "Netz empfangen", "KB/s", "io", 200, "Mehr Netzwerk-Download (Steam/Battle.net-Updates, Streams).")
m("net_tx", "Netz senden", "KB/s", "io", 100, "Mehr Upload (Streaming, Discord-Bildschirmübertragung, Cloud-Sync).")

m("game_running", "Spiel läuft", "", "game", 0.5, chart=False)
m("game_cpu", "Spiel CPU", "% (1 Kern=100)", "game", 15, None,
  "Das Spiel selbst bekommt/nutzt weniger CPU – es wird ausgebremst (warten auf GPU/IO/Speicher).")
m("game_rss", "Spiel RAM", "MB", "game", 300,
  "Das Spiel belegt immer mehr Arbeitsspeicher (mögliches Speicherleck, das mit der Spielzeit wächst).")
m("game_threads", "Spiel Threads", "", "game", 10, "Das Spiel hat deutlich mehr Threads als sonst.")
m("game_min", "Minuten seit Spielstart", "min", "game", 99999, chart=False)
m("wine_cpu", "wineserver CPU", "%", "game", 5,
  "wineserver arbeitet viel – Wine/Proton-Overhead. Andere Proton-Version oder `PROTON_NO_FSYNC` testen.")
m("shader_cpu", "Shader-Kompilierung (fossilize)", "%", "game", 5,
  "Steam kompiliert im Hintergrund Vulkan-Shader (fossilize_replay) und frisst dabei CPU. In Steam unter "
  "Einstellungen → Downloads/Shader-Vorkompilierung 'Verarbeitung von Vulkan-Shadern im Hintergrund' "
  "deaktivieren.")
m("fg_is_game", "Spiel im Vordergrund", "", "game", 0.5, chart=False)

m("uptime_h", "Uptime", "h", "misc", 99999, chart=False)
m("nprocs", "Prozesse", "", "misc", 20, "Deutlich mehr Prozesse laufen.")
m("nthreads", "Threads gesamt", "", "misc", 200, chart=False)


def floor(key):
    return M.get(key, {}).get("floor", 1.0)


def catalogue():
    return {"groups": [dict(key=k, label=l) for k, l in GROUPS], "metrics": list(M.values())}


# Hints for well-known processes
PROCESS_HINTS = {
    "fossilize_replay": "Steam-Shader-Vorkompilierung. In Steam die Hintergrundverarbeitung von Vulkan-Shadern abschalten.",
    "baloo_file": "KDE-Dateiindexer. Mit `balooctl6 suspend` (bzw. `balooctl disable`) testweise abschalten.",
    "baloo_file_extr": "KDE-Dateiindexer. Mit `balooctl6 suspend` (bzw. `balooctl disable`) testweise abschalten.",
    "tracker-miner-f": "GNOME-Dateiindexer (tracker). Testweise deaktivieren.",
    "packagekitd": "Paketverwaltung sucht/installiert Updates im Hintergrund.",
    "discover": "KDE Discover (Updates) läuft im Hintergrund.",
    "gnome-software": "GNOME Software (Updates) läuft im Hintergrund.",
    "steamwebhelper": "Steam-Oberfläche (Chromium). Steam-Overlay oder Steam-Fenster kann CPU/GPU fressen.",
    "Discord": "Discord – Hardwarebeschleunigung in Discord testweise deaktivieren.",
    "discord": "Discord – Hardwarebeschleunigung in Discord testweise deaktivieren.",
    "obs": "OBS – Aufnahme/Streaming kostet Leistung.",
    "kwin_wayland": "Der Compositor (KWin). Viel CPU hier → Fensterverwaltung/Effekte bremsen.",
    "kwin_x11": "Der Compositor (KWin). Viel CPU hier → Fensterverwaltung/Effekte bremsen.",
    "gnome-shell": "Der Compositor (GNOME Shell). Viel CPU hier → Erweiterungen/Effekte prüfen.",
    "Xwayland": "Xwayland – das Spiel läuft über Xwayland; viel CPU hier kann Ruckler verursachen.",
    "wineserver": "Wine/Proton-Server. Viel CPU → andere Proton-Version testen.",
    "kswapd0": "Kernel lagert Speicher aus – RAM wird knapp.",
    "kcompactd0": "Kernel räumt Speicher um (Compaction) – siehe THP-Hinweis.",
    "irq/": "Hardware-Interrupts.",
    "firefox": "Browser im Hintergrund (Videos/Tabs können GPU/CPU nutzen).",
    "chrome": "Browser im Hintergrund (Videos/Tabs können GPU/CPU nutzen).",
    "Battle.net.exe": "Battle.net-Launcher – lädt evtl. Updates.",
    "Agent.exe": "Blizzard-Update-Agent – lädt/prüft evtl. Spieldateien.",
    "steam": "Steam-Client (Downloads, Shader-Updates).",
}


def process_hint(name):
    if name in PROCESS_HINTS:
        return PROCESS_HINTS[name]
    for k, v in PROCESS_HINTS.items():
        if name.startswith(k) or name.lower().startswith(k.lower()):
            return v
    return None


KERNEL_HINTS = [
    ("split lock", "Split-Lock-Erkennung des Kernels bremst das Spiel absichtlich aus. "
                   "Test: `sudo sysctl kernel.split_lock_mitigate=0` (dauerhaft per /etc/sysctl.d/)."),
    ("ring gfx", "Die AMD-GPU meldet Timeouts/Hänger (ring gfx timeout) – Treiber/Kernel/Mesa aktualisieren."),
    ("amdgpu", "Meldungen des AMD-Grafiktreibers."),
    ("nvrm", "Meldungen des NVIDIA-Treibers (Xid-Fehler deuten auf Treiber-/Hardwareprobleme)."),
    ("xid", "NVIDIA-Xid-Fehler – Treiber-/Hardwareproblem."),
    ("thermal", "Thermische Meldungen – Temperatur prüfen."),
    ("throttl", "Drosselung gemeldet."),
    ("out of memory", "Der Speicher war voll (OOM-Killer)."),
    ("usb", "USB-Geräte (dis)connects – wackelige Geräte können Interrupt-Stürme auslösen."),
    ("hung task", "Ein Prozess hing länger in einer Kernel-Operation."),
]


def kernel_hint(text):
    t = text.lower()
    for k, v in KERNEL_HINTS:
        if k in t:
            return v
    return None
