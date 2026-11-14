"""What a container launch actually enforces, observed from inside (ADR 0013 D10, D16; #270).

Runs *inside* a container, as the agent would, and prints one JSON object:
what it observed, never a judgement and never a credential. The host
judges it (:func:`autoforge.sandbox_container.judge`), so a mistake here
cannot pass a runtime that enforces less: a field it could not read is
empty, and an empty field proves nothing.

It imports nothing of :mod:`autoforge` and only the standard library, so
the image can carry this file alone and run it with ``python3 -I``. Its one
argument is a JSON object:

- ``places``: directories whose writability the agent tests, by creating
  and removing a file of its own in each;
- ``connect_hosts`` and ``connect_ports``: addresses that could lead to the
  host's loopback (the container's own ``127.0.0.1`` and ``::1``, which a
  port forwarder could carry out, the slirp4netns and pasta host aliases,
  the runtimes' host names), to which the default gateways are added; each
  attempt records what it received, so the host can tell its own
  listener's nonce from anything else;
- ``abstract``: the name of a host abstract Unix socket to connect to;
- ``keyring``: the description of a ``user`` key the host put in its
  session keyring, searched for in this process's keyrings;
- ``sentinels``: fake credential values, reported by index wherever they
  are found (an environment variable as ``env:NAME``, a file by its path),
  so the observation never repeats one;
- ``scan_roots`` and ``scan_skip``: where the sentinel search walks, and
  what it leaves out.

What it reads: ``/proc/self/status`` (UIDs, GIDs, capabilities,
``NoNewPrivs``, ``Seccomp``), ``/proc/self/uid_map``,
``/proc/self/mountinfo``, the cgroup's ``pids.max``, ``/proc/net/dev``,
``/proc/net/route`` and ``/proc/net/ipv6_route``. The UID map it prints is
relative to the parent user namespace, which is not the host's when the
runtime nests one; the judge reads the map the host sees instead.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import socket
import struct
import sys

_TIMEOUT_SECONDS = 2.0
_RECEIVE_BYTES = 128
_MAX_SCANNED_BYTES = 1024 * 1024
_MAX_HITS = 20
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
_HOST_ALIASES = (
    "127.0.0.1",
    "::1",
    "10.0.2.2",
    "169.254.1.2",
    "host.containers.internal",
    "host.docker.internal",
)


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


def _gateways() -> list[str]:
    found = []
    for line in _read("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
            # The kernel prints the address's four bytes as one host-order word.
            raw = struct.pack("=I", int(parts[2], 16))
            found.append(socket.inet_ntop(socket.AF_INET, raw))
    for line in _read("/proc/net/ipv6_route").splitlines():
        parts = line.split()
        if len(parts) == 10 and parts[0] == "0" * 32 and parts[4] != "0" * 32:
            found.append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(parts[4])))
    return found


def _receive(sock: socket.socket) -> str:
    sock.settimeout(_TIMEOUT_SECONDS)
    try:
        return sock.recv(_RECEIVE_BYTES).decode("ascii", "replace")
    except OSError:
        return ""


def _connect(host: str, port: int) -> dict[str, object]:
    attempt: dict[str, object] = {"host": host, "port": port}
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT_SECONDS) as sock:
            attempt["result"] = "connected"
            attempt["received"] = _receive(sock)
    except OSError as exc:
        attempt["result"] = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    return attempt


def _abstract(name: str) -> dict[str, object]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect("\0" + name)
            return {"result": "connected", "received": _receive(sock)}
    except OSError as exc:
        return {"result": errno.errorcode.get(exc.errno or 0, type(exc).__name__)}


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


def _skipped(path: str, skip: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in skip)


def _sentinels(values: list[str], roots: list[str], skip: list[str]) -> dict[str, list[str]]:
    needles = [value.encode() for value in values]
    hits: dict[str, list[str]] = {str(index): [] for index in range(len(values))}

    def record(index: int, place: str) -> None:
        if len(hits[str(index)]) < _MAX_HITS:
            hits[str(index)].append(place)

    for name, value in sorted(os.environ.items()):
        for index, text in enumerate(values):
            if text in value:
                record(index, f"env:{name}")
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _skipped(os.path.join(directory, d), skip))
            for name in sorted(files):
                path = os.path.join(directory, name)
                if _skipped(path, skip):
                    continue
                try:
                    if not os.path.isfile(path) or os.path.islink(path):
                        continue
                    if os.path.getsize(path) > _MAX_SCANNED_BYTES:
                        continue
                    with open(path, "rb") as handle:
                        data = handle.read(_MAX_SCANNED_BYTES)
                except OSError:
                    continue
                for index, needle in enumerate(needles):
                    if needle in data:
                        record(index, path)
    return hits


def _strings(spec: dict[str, object], key: str, default: tuple[str, ...] = ()) -> list[str]:
    value = spec.get(key, list(default))
    return [str(item) for item in value] if isinstance(value, list) else list(default)


def observe(spec: dict[str, object]) -> dict[str, object]:
    """What this process sees, for the host to judge."""
    places = _strings(spec, "places")
    ports = [int(port) for port in _strings(spec, "connect_ports")]
    hosts = _strings(spec, "connect_hosts", _HOST_ALIASES)
    for gateway in _gateways():
        if gateway not in hosts:
            hosts.append(gateway)
    pids_max = _read("/sys/fs/cgroup/pids.max") or _read("/sys/fs/cgroup/pids/pids.max")
    observation: dict[str, object] = {
        "status": _read("/proc/self/status"),
        "uid_map": _read("/proc/self/uid_map"),
        "mountinfo": _read("/proc/self/mountinfo"),
        "pids_max": pids_max,
        "writable": {place: _writable(place) for place in places},
        "connect": [_connect(host, port) for port in ports for host in hosts],
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
        observation["sentinels"] = _sentinels(sentinels, roots, skip)
    return observation


def main(argv: list[str]) -> int:
    spec = json.loads(argv[1]) if len(argv) > 1 else {}
    if not isinstance(spec, dict):
        print("the measurement takes one JSON object", file=sys.stderr)
        return 2
    print(json.dumps(observe(spec), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
