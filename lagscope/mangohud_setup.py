"""Linux: install MangoHud from the web UI.

Detects how MangoHud fits this system and runs the matching package manager. Root rights are asked for with
`pkexec` – the system's own graphical password dialog (Lagscope never sees the password). If Steam is a Flatpak,
the Flatpak variant of MangoHud is installed instead (no password needed) and Steam is allowed to write into
Lagscope's folder.
"""
import os
import shutil
import subprocess
import threading
import time

from . import config

FLATPAK_STEAM = "com.valvesoftware.Steam"
FLATPAK_LAYER = "org.freedesktop.Platform.VulkanLayer.MangoHud"

_job = {"running": False, "ok": None, "log": "", "started": None, "finished": None}
_lock = threading.Lock()


def _os_release():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                info[k] = v.strip('"')
    except OSError:
        pass
    return info


def _run(args, timeout=20):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _flatpak_has(ref):
    r = _run(["flatpak", "info", ref])
    return bool(r and r.returncode == 0)


def _steam_runtime_branch():
    """Branch of the Freedesktop runtime Flatpak-Steam uses (the MangoHud extension must match it)."""
    r = _run(["flatpak", "info", "--show-runtime", FLATPAK_STEAM])
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    return None


def plan():
    """What is installed, and what the install button would do."""
    osr = _os_release()
    ids = {osr.get("ID", "")} | set(osr.get("ID_LIKE", "").split())
    native = bool(shutil.which("mangohud"))
    flatpak = bool(shutil.which("flatpak"))
    fp_steam = flatpak and _flatpak_has(FLATPAK_STEAM)
    fp_layer = False
    if fp_steam:
        r = _run(["flatpak", "list", "--columns=application"])
        fp_layer = bool(r and FLATPAK_LAYER in r.stdout)
    native_steam = bool(shutil.which("steam"))
    out = dict(native=native, flatpak_steam=fp_steam, flatpak_layer=fp_layer, native_steam=native_steam,
               os=osr.get("PRETTY_NAME", "Linux"), can_install=False, method=None, command=None, hint=None)

    need_native = not native and (native_steam or not fp_steam)
    need_flatpak = fp_steam and not fp_layer
    if not need_native and not need_flatpak:
        out["hint"] = "MangoHud ist installiert."
        return out

    if need_flatpak:
        branch = _steam_runtime_branch()
        ref = FLATPAK_LAYER + (f"//{branch}" if branch else "")
        out.update(can_install=True, method="flatpak",
                   command=f"flatpak install --user -y --noninteractive flathub {ref}")
        if not need_native:
            return out

    if "steamos" in ids:
        out.update(can_install=out["can_install"], hint="SteamOS: das Systemlaufwerk ist schreibgeschützt. MangoHud "
                   "ist auf SteamOS normalerweise schon vorhanden – sonst über den Discover-Store (Flatpak) installieren.")
        return out
    if os.path.exists("/run/ostree-booted"):
        cmd = "rpm-ostree install mangohud"
        out.update(hint="Unveränderliches System (z. B. Silverblue/Kinoite/Bazzite): MangoHud lässt sich nur mit Neustart "
                   f"installieren – im Terminal: {cmd} und danach neu starten.", command=out["command"] or cmd)
        return out
    if shutil.which("apt-get"):
        cmd = "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y mangohud"
    elif shutil.which("dnf"):
        cmd = "dnf install -y mangohud"
    elif shutil.which("pacman"):
        # lib32-mangohud nur, wenn multilib aktiv ist – sonst ohne
        cmd = ("pacman -S --noconfirm --needed mangohud lib32-mangohud || "
               "pacman -S --noconfirm --needed mangohud")
    elif shutil.which("zypper"):
        cmd = "zypper -n install mangohud"
    else:
        out["hint"] = "Paketverwaltung nicht erkannt – MangoHud bitte über das Software-Center installieren."
        return out
    if need_flatpak:  # beides nötig (Steam nativ und als Flatpak)
        out["command"] = out["command"] + " ; " + cmd
        out["method"] = "flatpak+system"
    else:
        out.update(method="system", command=cmd)
    out["can_install"] = True
    if os.geteuid() != 0 and not shutil.which("pkexec"):
        out.update(can_install=False, hint=f"pkexec fehlt – bitte im Terminal ausführen: sudo sh -c '{cmd}'")
    return out


def job_status():
    with _lock:
        return dict(_job)


def _log(text):
    with _lock:
        _job["log"] = (_job["log"] + text)[-6000:]


def _exec(args, timeout=1800):
    _log("$ " + " ".join(args) + "\n")
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    t0 = time.time()
    for line in p.stdout:
        _log(line)
        if time.time() - t0 > timeout:
            p.kill()
            _log("\nAbgebrochen (Zeitüberschreitung).\n")
            break
    return p.wait()


def _install(pl):
    ok = True
    try:
        if pl["method"] in ("flatpak", "flatpak+system"):
            fp_cmd = pl["command"].split(" ; ")[0].split()
            _exec(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub",
                   "https://dl.flathub.org/repo/flathub.flatpakrepo"])
            ok = _exec(fp_cmd) == 0 and ok
            # Flatpak-Steam darf sonst nicht in den Lagscope-Ordner schreiben
            ok = _exec(["flatpak", "override", "--user", f"--filesystem={config.DATA_DIR}", FLATPAK_STEAM]) == 0 and ok
        if pl["method"] in ("system", "flatpak+system"):
            cmd = pl["command"].split(" ; ")[-1]
            args = ["sh", "-c", cmd] if os.geteuid() == 0 else ["pkexec", "sh", "-c", cmd]
            rc = _exec(args)
            if rc in (126, 127) and os.geteuid() != 0:
                _log("\nPasswortabfrage abgebrochen oder nicht möglich.\n")
            ok = rc == 0 and ok
    except Exception as e:
        _log(f"\nFehler: {e!r}\n")
        ok = False
    with _lock:
        _job.update(running=False, ok=ok, finished=time.time())
    _log("\nFertig – MangoHud ist installiert.\n" if ok else "\nNicht erfolgreich – siehe Ausgabe oben.\n")


def start_install():
    pl = plan()
    if not pl["can_install"]:
        return dict(ok=False, error=pl["hint"] or "Nichts zu installieren.")
    with _lock:
        if _job["running"]:
            return dict(ok=False, error="Installation läuft bereits.")
        _job.update(running=True, ok=None, log="", started=time.time(), finished=None)
    threading.Thread(target=_install, args=(pl,), daemon=True, name="mangohud-install").start()
    return dict(ok=True)
