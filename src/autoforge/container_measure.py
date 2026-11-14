"""What a container launch actually enforces, observed from inside (ADR 0013 D10, D16; #270).

Runs *inside* a container, as the agent would, and prints one JSON object:
what it observed, never a judgement and never a credential. The host
judges it (:func:`autoforge.sandbox_container.judge`), so a mistake here
cannot pass a runtime that enforces less: a field it could not read is
empty, an attempt it could not complete says so, and neither proves
anything.

It imports nothing of :mod:`autoforge` and only the standard library, so
the image can carry this file alone and run it with ``python3 -I``. It
reads its request, one JSON object, from **standard input**, never from an
argument: the request holds the fake credentials it searches for, and an
argument is on a command line every local process can read (and on the
runtime's ``exec`` argv). A request given as an argument is refused. Its
fields:

- ``places``: directories whose writability the agent tests, by creating
  and removing a file of its own in each;
- ``connect_hosts`` and ``connect_ports``: addresses and names that could
  lead to the host's loopback (the container's own ``127.0.0.1`` and
  ``::1``, which a port forwarder could carry out, the runtimes' host
  aliases and host addresses), to which every default gateway is added.
  Each is resolved, and each address it resolves to tried, recording what
  came back: a refusal, a timeout, or a connection, with what it received
  and whether the peer closed, so the host can tell its own listener's
  nonce, another service and an inconclusive attempt apart;
- ``abstract``: the name of a host abstract Unix socket to connect to;
- ``keyring``: the description of a ``user`` key the host put in its
  session keyring, searched for in this process's keyrings;
- ``sentinels``: fake credential values, reported by index wherever they
  are found (an environment variable as ``env:NAME``, a file by its path),
  so the observation never repeats one;
- ``scan_roots``, ``scan_skip`` and ``scan_max_bytes``: where the sentinel
  search walks, what it leaves out, and how many bytes it reads at most.
  Every regular file is read whole, in chunks; the search reports its
  coverage (``scan``): what it could not cover is ``incomplete``, which the
  judge refuses, and only a file or directory the agent is denied and does
  not own is ``denied``, being out of the agent's reach.

What it reads: ``/proc/self/status`` (UIDs, GIDs, capabilities,
``NoNewPrivs``, ``Seccomp``), ``/proc/self/uid_map``,
``/proc/self/mountinfo``, the listing of each tmpfs, mqueue and devpts
mounted from its own root (``listings``: names, each one's kind and each
link's target), each mount point's device and inode by ``lstat``
(``identities``, never a file's contents), the cgroup's ``pids.max``,
``/proc/net/dev``,
``/proc/net/route`` and ``/proc/net/ipv6_route`` (``gateways`` is ``null``
when a routing table could not be read). The UID map it prints is
relative to the parent user namespace, which is not the host's when the
runtime nests one; the judge reads the map the host sees instead.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import socket
import stat
import struct
import sys
import time
from typing import TextIO

_TIMEOUT_SECONDS = 2.0
_RECEIVE_BYTES = 512
_MAX_REQUEST_CHARS = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_DEFAULT_SCAN_MAX_BYTES = 16 * 1024 * 1024 * 1024
_MAX_HITS = 20
_MAX_REPORTED = 20
# How many entries of one listed filesystem are named, with their kinds: a
# runtime's /dev holds about fifteen; a count above what was named fails.
_MAX_LISTED = 64
# The filesystems listed when mounted from their own root: a fresh one and a
# host one bound in whole look alike in mountinfo, and only what they hold
# tells them apart.
_LISTED_TYPES = frozenset({"tmpfs", "mqueue", "devpts"})
_KINDS = (
    (stat.S_ISLNK, "link"),
    (stat.S_ISDIR, "dir"),
    (stat.S_ISREG, "file"),
    (stat.S_ISCHR, "char"),
    (stat.S_ISBLK, "block"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"),
)
# keyctl(2)'s number per architecture; KEYCTL_SEARCH; the special keyrings.
_SYS_KEYCTL = {
    "x86_64": 250,
    "aarch64": 219,
    "riscv64": 219,
    "i686": 288,
    "i386": 288,
    "armv7l": 311,
    "ppc64le": 130,
    "s390x": 280,
}
_KEYCTL_SEARCH = 10
_KEYRINGS = {"session": -3, "user": -4, "user-session": -5}
# The default addresses and names to try, when the request names none: both
# loopback families, both runtimes' host aliases, and rootless Docker's host
# address (``autoforge.sandbox_container.loopback_routes``).
_HOST_ROUTES = (
    "127.0.0.1",
    "::1",
    "host.containers.internal",
    "host.docker.internal",
    "10.0.2.2",
)
_EAI_NAMES = {
    getattr(socket, name): name
    for name in dir(socket)
    if name.startswith("EAI_") and isinstance(getattr(socket, name), int)
}
# A file or directory the agent is denied: out of its reach unless it owns it.
_DENIED = (errno.EACCES, errno.EPERM)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _writable(place: str) -> bool:
    probe = os.path.join(place, f".autoforge-measure-{os.getpid()}")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def _lines(path: str, *, missing: list[str] | None = None) -> list[str] | None:
    """The lines of ``path``: ``missing`` when it does not exist, ``None`` when unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return missing
    except OSError:
        return None


def _gateways() -> list[str] | None:
    """Every default gateway; ``None`` when a routing table could not be read.

    ``/proc/net/route`` always has its header line, so an empty one was not
    read; ``/proc/net/ipv6_route`` is absent only from a kernel without IPv6.
    A table not read is not a table without a gateway.
    """
    ipv4 = _lines("/proc/net/route")
    ipv6 = _lines("/proc/net/ipv6_route", missing=[])
    if not ipv4 or ipv6 is None:
        return None
    found = []
    for line in ipv4[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
            # The kernel prints the address's four bytes as one host-order word.
            raw = struct.pack("=I", int(parts[2], 16))
            found.append(socket.inet_ntop(socket.AF_INET, raw))
    for line in ipv6:
        parts = line.split()
        if len(parts) == 10 and parts[0] == "0" * 32 and parts[4] != "0" * 32:
            address = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(parts[4]))
            # A link-local gateway is reachable only through its interface.
            if address.lower().startswith("fe80:"):
                address = f"{address}%{parts[9]}"
            found.append(address)
    return found


def _error(exc: OSError) -> str:
    if isinstance(exc, socket.timeout):
        return "timeout"
    return errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def _receive(sock: socket.socket) -> tuple[str, bool]:
    """What the peer sends within the timeout, and whether it closed (EOF) by then."""
    data = b""
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    try:
        while len(data) < _RECEIVE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            chunk = sock.recv(_RECEIVE_BYTES - len(data))
            if not chunk:
                return data.decode("ascii", "replace"), True
            data += chunk
    except OSError:
        pass
    return data.decode("ascii", "replace"), False


def _attempt(family: int, kind: int, proto: int, address: tuple) -> dict[str, object]:
    record: dict[str, object] = {"address": str(address[0])}
    try:
        with socket.socket(family, kind, proto) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect(address)
            record["result"] = "connected"
            record["received"], record["eof"] = _receive(sock)
    except OSError as exc:
        record["result"] = _error(exc)
    return record


def _route(host: str, port: int) -> dict[str, object]:
    """``host`` resolved, and each address it gives tried once."""
    entry: dict[str, object] = {"host": host, "port": port}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        entry["resolve_error"] = _EAI_NAMES.get(exc.errno, f"gaierror {exc.errno}")
        return entry
    except (OSError, UnicodeError) as exc:
        entry["resolve_error"] = type(exc).__name__
        return entry
    attempts = []
    seen = set()
    for family, kind, proto, _, address in infos:
        if (family, address) in seen:
            continue
        seen.add((family, address))
        attempts.append(_attempt(family, kind, proto, address))
    entry["attempts"] = attempts
    return entry


def _abstract(name: str) -> dict[str, object]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect("\0" + name)
            received, eof = _receive(sock)
            return {"result": "connected", "received": received, "eof": eof}
    except OSError as exc:
        return {"result": _error(exc)}


def _keyring(description: str) -> dict[str, str]:
    number = _SYS_KEYCTL.get(platform.machine())
    if number is None:
        return {"session": f"unknown architecture {platform.machine()}"}
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
    except (ImportError, OSError) as exc:
        return {"session": f"no keyctl call ({type(exc).__name__})"}
    libc.syscall.restype = ctypes.c_long
    results = {}
    for where, keyring in _KEYRINGS.items():
        ret = libc.syscall(
            number,
            _KEYCTL_SEARCH,
            ctypes.c_long(keyring),
            b"user",
            description.encode(),
            ctypes.c_long(0),
        )
        if ret >= 0:
            results[where] = "found"
        else:
            results[where] = errno.errorcode.get(ctypes.get_errno(), "unknown error")
    return results


def _interfaces() -> list[str]:
    names = []
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, sep, _ = line.partition(":")
        if sep:
            names.append(name.strip())
    return names


def _routes() -> list[str]:
    routes = []
    for line in _read("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] != "lo":
            routes.append(f"ipv4 via {parts[0]}")
    for line in _read("/proc/net/ipv6_route").splitlines():
        parts = line.split()
        if len(parts) == 10 and parts[9] != "lo":
            routes.append(f"ipv6 via {parts[9]}")
    return routes


def _unescape(field: str) -> str:
    """A ``mountinfo`` field with its octal escapes (``\\040`` for a space) undone."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mount_lines(mountinfo: str) -> list[tuple[str, str, str]]:
    """Each ``mountinfo`` line that parses, as its root, mount point and type."""
    found = []
    for line in mountinfo.splitlines():
        parts = line.split()
        if "-" not in parts[6:]:
            continue
        separator = parts.index("-", 6)
        if separator + 1 < len(parts):
            found.append((_unescape(parts[3]), _unescape(parts[4]), parts[separator + 1]))
    return found


def _kind(path: str) -> tuple[str, str | None]:
    """What ``path`` is, by ``lstat`` (the error that kept it unknown), and a link's target."""
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        return _error(exc), None
    kind = next((name for test, name in _KINDS if test(mode)), "other")
    if kind != "link":
        return kind, None
    try:
        return kind, os.readlink(path)
    except OSError as exc:
        return kind, f"unread ({_error(exc)})"


def _listings(mountinfo: str) -> dict[str, dict[str, object]]:
    """What each tmpfs, mqueue and devpts mounted from its own root holds, by mount point.

    A fresh one the runtime makes is empty, or holds only what the runtime
    or the policy puts in it; a host one bound in whole looks the same in
    ``mountinfo``, but holds the host's files. So each is listed: its entry
    count, its first names with each one's kind (``kinds``, by ``lstat``)
    and each link's target (``links``), or the error that kept it from
    being listed.
    """
    contents: dict[str, dict[str, object]] = {}
    for root, point, fstype in _mount_lines(mountinfo):
        if fstype not in _LISTED_TYPES or root != "/":
            continue
        try:
            names = sorted(os.listdir(point))
        except OSError as exc:
            contents[point] = {"error": _error(exc)}
            continue
        shown = names[:_MAX_LISTED]
        kinds: dict[str, str] = {}
        links: dict[str, str] = {}
        for name in shown:
            kinds[name], target = _kind(os.path.join(point, name))
            if target is not None:
                links[name] = target
        contents[point] = {"count": len(names), "names": shown, "kinds": kinds, "links": links}
    return contents


def _identities(mountinfo: str) -> dict[str, object]:
    """Each mount point's device (major, minor) and inode, by ``lstat``, or the error.

    What a bind's target shows is its host source's own device and inode,
    which the host compares with the source it meant (metadata only: no
    file is read).
    """
    found: dict[str, object] = {}
    for _, point, _ in _mount_lines(mountinfo):
        if point in found:
            continue
        try:
            info = os.lstat(point)
        except OSError as exc:
            found[point] = _error(exc)
            continue
        found[point] = [os.major(info.st_dev), os.minor(info.st_dev), info.st_ino]
    return found


def _skipped(path: str, skip: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in skip)


class _BudgetSpent(Exception):
    """The search read as many bytes as it may: what is left is not covered."""


class _Scan:
    """One sentinel search: its hits by index, and what it could and could not cover."""

    def __init__(self, values: list[str], max_bytes: int) -> None:
        self.values = values
        self.needles = [value.encode() for value in values]
        self.overlap = max(len(needle) for needle in self.needles) - 1
        self.max_bytes = max_bytes
        self.hits: dict[str, list[str]] = {str(index): [] for index in range(len(values))}
        self.files = 0
        self.bytes = 0
        self.denied = 0
        self.special = 0
        self.incomplete: list[str] = []
        self.incomplete_count = 0

    def record(self, index: int, place: str) -> None:
        if len(self.hits[str(index)]) < _MAX_HITS:
            self.hits[str(index)].append(place)

    def gap(self, path: str, why: str) -> None:
        self.incomplete_count += 1
        if len(self.incomplete) < _MAX_REPORTED:
            self.incomplete.append(f"{path}: {why}")

    def refused(self, path: str, exc: OSError, searchable: bool) -> None:
        """A path the agent could not open: denied and not its own is out of its reach."""
        try:
            owned = os.lstat(path).st_uid == os.getuid()
        except OSError:
            owned = True
        if exc.errno in _DENIED and not owned and not searchable:
            self.denied += 1
        else:
            self.gap(path, _error(exc))

    def environment(self) -> None:
        for name, value in sorted(os.environ.items()):
            for index, text in enumerate(self.values):
                if text in value:
                    self.record(index, f"env:{name}")

    def file(self, path: str) -> None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            self.gap(path, _error(exc))
            return
        if stat.S_ISLNK(info.st_mode):
            return  # its target is searched where it lies
        if not stat.S_ISREG(info.st_mode):
            self.special += 1  # a FIFO, socket or device node stores nothing
            return
        try:
            handle = open(path, "rb")  # noqa: SIM115 - closed below, chunks read in a loop
        except OSError as exc:
            self.refused(path, exc, searchable=False)
            return
        found: set[int] = set()
        tail = b""
        try:
            with handle:
                while True:
                    chunk = handle.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > self.max_bytes:
                        self.gap(path, f"the search's {self.max_bytes}-byte budget is spent")
                        raise _BudgetSpent
                    window = tail + chunk
                    for index, needle in enumerate(self.needles):
                        if index not in found and needle in window:
                            found.add(index)
                    tail = window[-self.overlap :] if self.overlap else b""
        except OSError as exc:
            self.gap(path, _error(exc))
        self.files += 1
        for index in sorted(found):
            self.record(index, path)

    def walk(self, roots: list[str], skip: list[str]) -> None:
        def unlisted(exc: OSError) -> None:
            path = exc.filename or "?"
            self.refused(path, exc, searchable=os.access(path, os.X_OK))

        for root in roots:
            for directory, dirs, files in os.walk(root, onerror=unlisted, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not _skipped(os.path.join(directory, d), skip))
                for name in sorted(files):
                    path = os.path.join(directory, name)
                    if not _skipped(path, skip):
                        self.file(path)

    def report(self, roots: list[str], skip: list[str]) -> dict[str, object]:
        return {
            "roots": roots,
            "skip": skip,
            "files": self.files,
            "bytes": self.bytes,
            "denied": self.denied,
            "special": self.special,
            "incomplete": self.incomplete,
            "incomplete_count": self.incomplete_count,
        }


def _search(
    values: list[str], roots: list[str], skip: list[str], max_bytes: int
) -> tuple[dict[str, list[str]], dict[str, object]]:
    """Where each sentinel is, and what the search covered."""
    scan = _Scan(values, max_bytes)
    scan.environment()
    try:
        scan.walk(roots, skip)
    except _BudgetSpent:
        pass
    return scan.hits, scan.report(roots, skip)


def _strings(spec: dict[str, object], key: str, default: tuple[str, ...] = ()) -> list[str]:
    value = spec.get(key, list(default))
    return [str(item) for item in value] if isinstance(value, list) else list(default)


def observe(spec: dict[str, object]) -> dict[str, object]:
    """What this process sees, for the host to judge."""
    places = _strings(spec, "places")
    ports = [int(port) for port in _strings(spec, "connect_ports")]
    hosts = _strings(spec, "connect_hosts", _HOST_ROUTES)
    gateways = _gateways()
    for gateway in gateways or []:
        if gateway not in hosts:
            hosts.append(gateway)
    pids_max = _read("/sys/fs/cgroup/pids.max") or _read("/sys/fs/cgroup/pids/pids.max")
    mountinfo = _read("/proc/self/mountinfo")
    observation: dict[str, object] = {
        "status": _read("/proc/self/status"),
        "uid_map": _read("/proc/self/uid_map"),
        "mountinfo": mountinfo,
        "pids_max": pids_max,
        "writable": {place: _writable(place) for place in places},
        # After the writability probes, which remove what they create.
        "listings": _listings(mountinfo),
        "identities": _identities(mountinfo),
        "gateways": gateways,
        "connect": [_route(host, port) for port in ports for host in hosts],
        "interfaces": _interfaces(),
        "routes": _routes(),
    }
    abstract = spec.get("abstract")
    if isinstance(abstract, str) and abstract:
        observation["abstract"] = _abstract(abstract)
    description = spec.get("keyring")
    if isinstance(description, str) and description:
        observation["keyring"] = _keyring(description)
    sentinels = _strings(spec, "sentinels")
    if sentinels:
        roots = _strings(spec, "scan_roots", ("/",))
        skip = _strings(spec, "scan_skip", ("/proc", "/sys", "/dev"))
        limit = spec.get("scan_max_bytes", _DEFAULT_SCAN_MAX_BYTES)
        max_bytes = limit if isinstance(limit, int) and limit > 0 else _DEFAULT_SCAN_MAX_BYTES
        hits, scan = _search(sentinels, roots, skip, max_bytes)
        observation["sentinels"] = hits
        observation["scan"] = scan
    return observation


def main(argv: list[str], stdin: TextIO | None = None) -> int:
    """Read the request from standard input, print the observation; 2 for a bad request."""
    if len(argv) > 1:
        print(
            "the measurement reads its request from standard input, never an argument, "
            "so that no fake credential is on a command line",
            file=sys.stderr,
        )
        return 2
    text = (stdin or sys.stdin).read(_MAX_REQUEST_CHARS + 1)
    try:
        spec = json.loads(text) if len(text) <= _MAX_REQUEST_CHARS else None
    except ValueError:
        spec = None
    if not isinstance(spec, dict):
        print("the measurement takes one JSON object on standard input", file=sys.stderr)
        return 2
    if any(not value for value in _strings(spec, "sentinels")):
        print("a sentinel is a non-empty string", file=sys.stderr)
        return 2
    print(json.dumps(observe(spec), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
