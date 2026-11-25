"""Run pytest with the project network, replay and data guards."""

import hashlib, json, os, subprocess, sys
from pathlib import Path

root = Path(__file__).resolve().parent
os.chdir(root)
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
protected, child = str(root / "data"), False
bootstrap = '''
import ipaddress, os, sys
blocked = []

def deny(event):
    blocked.append(event)
    print("OFFLINE GUARD BLOCKED", event, file=sys.stderr, flush=True)
    if child:
        os._exit(96)
    raise AssertionError(event)

def production(path, fd=-1):
    if isinstance(path, int):
        path = os.readlink(f"/proc/self/fd/{path}")
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    path = os.fsdecode(path)
    if not os.path.isabs(path) and fd not in (-1, None):
        path = os.path.join(os.readlink(f"/proc/self/fd/{fd}"), path)
    path = os.path.abspath(path)
    return path == protected or path.startswith(protected + os.sep)

def audit(event, args):
    host = None
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"):
        host = args[0]
    if event in ("socket.connect", "socket.sendto", "socket.bind") and isinstance(args[1], tuple):
        host = args[1][0]
    if host not in (None, "localhost", b"localhost"):
        try:
            local = ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
        except ValueError:
            local = False
        if not local:
            deny(event)
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        if production(args[0]):
            deny(event)
    if event == "os.rename" and (production(args[0], args[2]) or production(args[1], args[3])):
        deny(event)
    if event in ("os.remove", "os.rmdir") and production(args[0], args[1]):
        deny(event)
    if event in ("os.mkdir", "os.chmod") and production(args[0], args[2]):
        deny(event)

sys.addaudithook(audit)
'''
exec(bootstrap)
allowed = {str(root / name) for name in ("build_contracts.py", "calculate_greeks.py")}

class GuardedReplay(subprocess.Popen):
    total = 0

    def __init__(self, args, *pos, **kw):
        if not (isinstance(args, (list, tuple)) and len(args) == 5
                and list(args[:4]) == [sys.executable, "-I", "-S", "-B"] and args[4] in allowed
                and not kw.get("shell") and not kw.get("executable")):
            deny("unexpected subprocess")
        type(self).total += 1
        boot = (f"{protected=!r}\nchild=True\n" + bootstrap
                + '\nimport runpy\nrunpy.run_path(sys.argv[1], run_name="__main__")\n')
        super().__init__([*args[:4], "-c", boot, args[4]], *pos, **kw)

    def communicate(self, *pos, **kw):
        result = super().communicate(*pos, **kw)
        if self.returncode == 96:
            deny(f"child {result[1]!r}")
        return result

subprocess.Popen = GuardedReplay

def snapshot():
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path("data").rglob("*") if p.is_file()}

before = snapshot()
import pytest

result = pytest.main(["-p", "no:cacheprovider", "-q", "--tb=short", str(root)])
assert snapshot() == before, "Protected data changed"
print("OFFLINE_GUARD_ATTEMPTS", blocked, "GUARDED_REPLAY_SUBPROCESSES", GuardedReplay.total,
      "DATA_UNCHANGED", len(before))
sys.exit(result or bool(blocked))
