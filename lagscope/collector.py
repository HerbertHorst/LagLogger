"""Picks the collector for the running operating system."""
from .osutil import IS_WIN

if IS_WIN:
    from .collector_win import (Foreground, KernelLog, ProcessTracker, SystemSampler,  # noqa: F401
                                boot_id, uptime_s)
else:
    from .collector_linux import (Foreground, KernelLog, ProcessTracker, SystemSampler,  # noqa: F401
                                  boot_id, uptime_s)
from .common import ProcInfo, snapshot_list  # noqa: F401,E402
