"""Run pytest with the project network, replay and data guards."""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--inventory", type=Path, help="write the collected pytest node IDs")
options = parser.parse_args()
inventory = options.inventory.absolute() if options.inventory else None
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

# Run intentional denials in a separate interpreter so that the suite's
# guard log contains only unexpected attempts. The fixture never uses data/.
probe = '''
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory() as directory:
    protected = str(Path(directory) / "protected")
    target = Path(protected) / "fixture.txt"
    target.parent.mkdir()
    target.write_text("untouched", encoding="utf-8")
    child = False
    exec(sys.argv[1])

    def assert_blocked(event, operation):
        try:
            operation()
        except AssertionError as error:
            if error.args == (event,):
                return
            raise
        raise AssertionError(f"guard did not block {event}")

    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        # Exercise the connect audit event without emitting a network packet.
        assert_blocked("socket.connect", lambda: sys.audit(
            "socket.connect", connection, ("192.0.2.1", 443)))
    assert_blocked("open", lambda: target.write_text("changed", encoding="utf-8"))
    assert_blocked("os.remove", target.unlink)
    if target.read_text(encoding="utf-8") != "untouched":
        raise AssertionError("guard probe changed its fixture")
    # Disable just the fixture's path protection for TemporaryDirectory cleanup.
    protected = str(Path(directory) / "unused")
'''
preflight = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", probe, bootstrap],
                           capture_output=True, text=True, timeout=15)
if preflight.returncode:
    sys.stderr.write(preflight.stderr)
    sys.exit("Offline guard self-check failed")

def replay_bootstrap(protected_path):
    return (f"protected={str(protected_path)!r}\nchild=True\n" + bootstrap
            + '\nimport runpy\nrunpy.run_path(sys.argv[1], run_name="__main__")\n')


def replay_child_check(source, expected_event, label):
    with TemporaryDirectory() as directory:
        fixture_root = Path(directory)
        protected_fixture = fixture_root / "protected"
        protected_fixture.mkdir(mode=0o700)
        target = protected_fixture / "fixture.txt"
        target.write_text("untouched", encoding="utf-8")
        script = fixture_root / "replay_fixture.py"
        script.write_text(source.replace("__TARGET__", repr(str(target))), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", replay_bootstrap(protected_fixture), str(script)],
            capture_output=True, text=True, timeout=15)
        unchanged = target.is_file() and target.read_text(encoding="utf-8") == "untouched"
        no_unexpected_files = set(protected_fixture.iterdir()) == {target}
        destination_missing = not (fixture_root / "renamed.txt").exists()
    if (result.returncode != 96 or result.stdout.strip() != "replay started"
            or "replay continued" in result.stdout
            or f"OFFLINE GUARD BLOCKED {expected_event}" not in result.stderr
            or not unchanged or not no_unexpected_files or not destination_missing):
        sys.stderr.write(result.stderr)
        sys.exit(f"Offline replay child self-check ({label}) failed (exit {result.returncode})")
    print(f"OFFLINE_REPLAY_{label}_CHILD_SELF_CHECK passed", flush=True)


replay_child_check(
    "from pathlib import Path\n"
    "print('replay started', flush=True)\n"
    "try:\n"
    "    Path(__TARGET__).write_text('changed')\n"
    "finally:\n"
    "    print('replay continued', flush=True)\n",
    "open", "WRITE")
replay_child_check(
    "import socket, sys\n"
    "print('replay started', flush=True)\n"
    "with socket.socket() as connection:\n"
    "    connection.bind(('127.0.0.1', 0))\n"
    "    sys.audit('socket.connect', connection, ('192.0.2.1', 443))\n"
    "print('replay continued', flush=True)\n",
    "socket.connect", "CONNECT")
replay_child_check(
    "import socket, sys\n"
    "print('replay started', flush=True)\n"
    "with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:\n"
    "    connection.bind(('127.0.0.1', 0))\n"
    "    sys.audit('socket.sendto', connection, ('192.0.2.1', 443))\n"
    "print('replay continued', flush=True)\n",
    "socket.sendto", "SENDTO")
replay_child_check(
    "import socket, sys\n"
    "print('replay started', flush=True)\n"
    "with socket.socket() as connection:\n"
    "    sys.audit('socket.bind', connection, ('0.0.0.0', 0))\n"
    "print('replay continued', flush=True)\n",
    "socket.bind", "BIND")
replay_child_check(
    "import os\nfrom pathlib import Path\n"
    "print('replay started', flush=True)\n"
    "os.rename(__TARGET__, Path(__TARGET__).parent.parent / 'renamed.txt')\n"
    "print('replay continued', flush=True)\n",
    "os.rename", "RENAME_FROM_PROTECTED")
replay_child_check(
    "import os\nfrom pathlib import Path\n"
    "print('replay started', flush=True)\n"
    "target = Path(__TARGET__)\n"
    "source = target.parent.parent / 'source.txt'\n"
    "source.write_text('unprotected')\n"
    "os.rename(source, target.with_name('renamed.txt'))\n"
    "print('replay continued', flush=True)\n",
    "os.rename", "RENAME_INTO_PROTECTED")
replay_child_check(
    "import os\n"
    "print('replay started', flush=True)\n"
    "os.remove(__TARGET__)\n"
    "print('replay continued', flush=True)\n",
    "os.remove", "REMOVE")
replay_child_check(
    "import sys\n"
    "print('replay started', flush=True)\n"
    "sys.audit('socket.getaddrinfo', 'example.invalid', 443)\n"
    "print('replay continued', flush=True)\n",
    "socket.getaddrinfo", "DNS")
print("OFFLINE_GUARD_SELF_CHECK passed", flush=True)

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
        boot = replay_bootstrap(protected)
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

class Inventory:
    def pytest_collection_finish(self, session):
        ids = sorted(item.nodeid for item in session.items)
        inventory.write_text("".join(f"{node_id}\n" for node_id in ids), encoding="utf-8")
        print(f"COLLECTED_TEST_IDS {len(ids)}", flush=True)


result = pytest.main(["-p", "no:cacheprovider", "-q", "--tb=short", str(root)],
                     plugins=[Inventory()] if inventory else [])
assert snapshot() == before, "Protected data changed"
print("OFFLINE_GUARD_ATTEMPTS", blocked, "GUARDED_REPLAY_SUBPROCESSES", GuardedReplay.total,
      "DATA_UNCHANGED", len(before))
sys.exit(result or bool(blocked))
