"""Windows: icon in the notification area (system tray) – shows that Lagscope is running.

Double click: open the report. Right click: open report, mark lag / good / lag over, quit.
Pure ctypes, runs in its own thread with its own message loop.
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0000
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_LBUTTONUP, WM_CONTEXTMENU = 0x0203, 0x0205, 0x0202, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0, 0x800, 0x1
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x100, 0x2, 0x80

CMD_OPEN, CMD_LAG, CMD_GOOD, CMD_END, CMD_QUIT = 1, 2, 3, 4, 9


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPVOID]
user32.LoadIconW.restype = wt.HICON
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.GetModuleHandleW.restype = wt.HMODULE


class Tray:
    """callbacks: open(), mark(kind), quit()."""

    def __init__(self, icon_path, on_open, on_mark, on_quit):
        self.icon_path = icon_path
        self.on_open, self.on_mark, self.on_quit = on_open, on_mark, on_quit
        self.hwnd = None
        self.tip = "Lagscope – zeichnet auf"
        self.ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # Referenz behalten (sonst räumt Python sie weg)
        threading.Thread(target=self._loop, daemon=True, name="tray").start()

    # ------------------------------------------------------------------ window / icon
    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tip[:127]
        return nid

    def _add_icon(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _loop(self):
        try:
            hinst = k32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "LagscopeTray"
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(0, "LagscopeTray", "Lagscope", 0, 0, 0, 0, 0, None, None, hinst, None)
            self.hicon = None
            if self.icon_path and os.path.exists(self.icon_path):
                self.hicon = user32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, LOADFROMFILE|DEFAULTSIZE
            if not self.hicon:
                self.hicon = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
            self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self._add_icon()
        finally:
            self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _menu(self):
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, CMD_OPEN, "Auswertung öffnen")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_LAG, "Lag jetzt markieren")
        user32.AppendMenuW(m, MF_STRING, CMD_GOOD, "Läuft gut markieren")
        user32.AppendMenuW(m, MF_STRING, CMD_END, "Lag vorbei markieren")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_QUIT, "Lagscope beenden")
        user32.SetMenuDefaultItem(m, CMD_OPEN, 0)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(m)
        self._command(cmd)

    def _command(self, cmd):
        try:
            if cmd == CMD_OPEN:
                self.on_open()
            elif cmd in (CMD_LAG, CMD_GOOD, CMD_END):
                self.on_mark({CMD_LAG: "lag", CMD_GOOD: "good", CMD_END: "end"}[cmd])
            elif cmd == CMD_QUIT:
                self.on_quit()
        except Exception as e:  # ein Fehler im Menü darf den Logger nicht beenden
            print(f"Tray: {e!r}", flush=True)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev == WM_LBUTTONDBLCLK:
                self._command(CMD_OPEN)
            elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if msg == getattr(self, "taskbar_created", -1):  # Explorer wurde neu gestartet
            self._add_icon()
            return 0
        if msg == WM_CLOSE:
            nid = self._nid(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------ from other threads
    def set_tip(self, text):
        if not self.hwnd or text == self.tip:
            return
        self.tip = text
        nid = self._nid(NIF_TIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
