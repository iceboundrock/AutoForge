"""A fake container runtime CLI (``podman`` or ``docker``) for #271's tests. No test of its own.

:func:`install` writes this file as an executable named ``podman`` or
``docker`` into a ``bin`` directory, beside a ``state.json`` it reads and
writes on every call (the runtime client passes the fake no variable of
the test's: its environment is allow-listed). The state holds what the
runtime reports (``version``, ``info``, or a list ``infos`` consumed one
per ``info`` call, so a store can move between calls), its volumes,
containers and images with their labels, and scripted answers:

- ``fail``: ``{verb: [exit, stderr]}`` makes a verb fail (``create``,
  ``volume create``, ``volume inspect``, ``container inspect``, ``start``,
  ``version``, ...);
- ``fail_after``: ``{verb: [exit, stderr]}`` makes a create take effect and
  then fail, as a call whose answer was lost (a timeout, a dropped
  connection) does (``volume create``, ``create``);
- ``volume create`` of a name the runtime holds: Podman refuses it; Docker
  (the fake installed as ``docker``) succeeds and returns the volume it
  holds, its labels unchanged, as Docker Engine does;
- ``entrypoints``: ``{image id: entrypoint}``, what ``image inspect`` shows
  as ``.Config.Entrypoint`` (``null`` for an image not listed); a build
  inherits ``base_entrypoint`` unless its Containerfile clears it with
  ``ENTRYPOINT []``, and sets ``built_entrypoint`` whatever it says;
- ``teardown``: ``[exit, stdout, seconds]``, the answer of the run step's
  kill-and-prove exec (default: proved at once); ``seconds`` makes it hang;
- ``exec``: ``{helper word: [exit, stdout]}``, the answer of an ``exec``
  whose command holds that word (``stage``, ``prove``, ``head``); an exec
  with ``receive`` hashes its standard input; ``pack`` writes the file
  ``pack_file`` names; an exec of ``python3 -I -c`` with ``/source/nonce``
  reads the nonce through the container's bind (``shares_filesystem``
  false: nothing to read); any other exec runs its command on the host
  (the run step's stand-in) with the container's create-time variables
  (``--env`` of the create, valued from the create's client environment)
  and the exec's ``--env`` names taken from the client's environment.

Every call is appended to ``calls.jsonl``: its argv, its working
directory, the names of its environment and, for a client configuration
directory, what that directory held.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load() -> dict:
    return json.loads((HERE / "state.json").read_text())


def _save(state: dict) -> None:
    (HERE / "state.json").write_text(json.dumps(state))


def _record(argv: list[str]) -> None:
    config = os.environ.get("DOCKER_CONFIG", "")
    listing = sorted(os.listdir(config)) if config and os.path.isdir(config) else None
    # The environment the fake was started with: os.environ also holds what
    # Python's own C-locale coercion added (LC_CTYPE), which no caller passed.
    with open("/proc/self/environ", "rb") as handle:
        started = [item.split(b"=", 1)[0].decode() for item in handle.read().split(b"\0") if item]
    entry = {
        "argv": argv,
        "cwd": os.getcwd(),
        "env": sorted(started),
        "config_dir": config,
        "config_listing": listing,
        "env_values": {k: os.environ[k] for k in os.environ if k.startswith("AF_TEST_")},
    }
    with open(HERE / "calls.jsonl", "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _fail(state: dict, verb: str) -> None:
    failure = state.get("fail", {}).get(verb)
    if failure:
        sys.stderr.write(failure[1] + "\n")
        sys.exit(failure[0])


def _fail_after(state: dict, verb: str) -> int:
    """The create took effect: fail now if scripted to, as a lost answer does."""
    failure = state.get("fail_after", {}).get(verb)
    if failure:
        sys.stderr.write(failure[1] + "\n")
        return int(failure[0])
    return 0


def _docker() -> bool:
    return Path(__file__).name == "docker"


def _options(args: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    opts: dict[str, list[str]] = {}
    rest: list[str] = []
    for i, arg in enumerate(args):
        if arg.startswith("--") and "=" in arg:
            key, _, value = arg[2:].partition("=")
            opts.setdefault(key, []).append(value)
        elif arg.startswith("--"):
            opts.setdefault(arg[2:], []).append("")
        else:
            rest = args[i:]
            break
    return opts, rest


def _labels(values: list[str]) -> dict[str, str]:
    return dict(value.split("=", 1) for value in values)


def _not_found(kind: str, name: str) -> None:
    sys.stderr.write(f"Error: no such {kind}: {name}\n")
    sys.exit(1)


def main(argv: list[str]) -> int:
    _record(argv)
    state = _load()
    verb = argv[0] if argv else ""
    if verb in ("version", "info"):
        _fail(state, verb)
        if verb == "info" and state.get("infos"):
            info = state["infos"].pop(0)
            if not state["infos"]:
                state["infos"] = [info]
            _save(state)
            print(json.dumps(info))
        else:
            print(json.dumps(state[verb]))
        return 0
    if verb == "volume":
        sub, args = argv[1], argv[2:]
        if sub == "create":
            _fail(state, "volume create")
            opts, rest = _options(args)
            name = rest[0]
            if name in state["volumes"]:
                if _docker():
                    # Docker Engine re-uses a volume of that name, as it is.
                    print(name)
                    return 0
                sys.stderr.write(f"Error: volume with name {name} already exists\n")
                return 125
            state["volumes"][name] = {"labels": _labels(opts.get("label", []))}
            _save(state)
            failed = _fail_after(state, "volume create")
            if failed:
                return failed
            print(name)
            return 0
        if sub == "inspect":
            _fail(state, "volume inspect")
            name = args[-1]
            if name not in state["volumes"]:
                _not_found("volume", name)
            fmt = args[args.index("--format") + 1]
            print(json.dumps(state["volumes"][name]["labels"]) if "Labels" in fmt else name)
            return 0
        if sub == "rm":
            _fail(state, "volume rm")
            name = args[-1]
            if any(name in c["mounts"] for c in state["containers"].values()):
                sys.stderr.write(f"Error: volume {name} is being used\n")
                return 2
            state["volumes"].pop(name, None)
            _save(state)
            return 0
    if verb == "container" and argv[1] == "inspect":
        _fail(state, "container inspect")
        args = argv[2:]
        name = args[-1]
        if name not in state["containers"]:
            _not_found("container", name)
        fmt = args[args.index("--format") + 1]
        print(json.dumps(state["containers"][name]["labels"]) if "Labels" in fmt else name)
        return 0
    if verb == "create":
        _fail(state, "create")
        opts, rest = _options(argv[1:])
        name = opts["name"][0]
        if name in state["containers"]:
            sys.stderr.write(f'Error: the container name "{name}" is already in use\n')
            return 125
        mounts = {}
        for value in opts.get("mount", []):
            fields = dict(item.split("=", 1) for item in value.split(",") if "=" in item)
            mounts[fields["source"]] = fields["target"]
        state["containers"][name] = {
            "labels": _labels(opts.get("label", [])),
            "mounts": mounts,
            "network": opts.get("network", [""])[0],
            "env": opts.get("env", []),
            "env_values": {n: os.environ.get(n, "") for n in opts.get("env", [])},
            "image": rest[0],
            "command": rest[1:],
            "running": False,
            "options": argv[1:],
        }
        _save(state)
        return _fail_after(state, "create")
    if verb == "start":
        _fail(state, "start")
        name = argv[1]
        if name not in state["containers"]:
            _not_found("container", name)
        state["containers"][name]["running"] = True
        _save(state)
        return 0
    if verb == "rm":
        _fail(state, "rm")
        name = argv[-1]
        state["containers"].pop(name, None)
        _save(state)
        return 0
    if verb == "exec":
        return _exec(state, argv[1:])
    if verb in ("image", "images"):
        args = argv[1:]
        if args and args[0] == "inspect":
            image = args[-1]
            if image not in state["images"]:
                _not_found("image", image)
            fmt = args[args.index("--format") + 1]
            if "Entrypoint" in fmt:
                print(json.dumps(state.get("entrypoints", {}).get(image)))
            else:
                print(json.dumps(state["images"][image]) if "Labels" in fmt else image)
            return 0
        filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
        want = filters[0].split("=", 1)[1] if filters else ""
        key, _, value = want.partition("=")
        for image, labels in state["images"].items():
            if labels.get(key) == value:
                print(("" if verb == "images" else "sha256:") + image)
        return 0
    if verb == "build":
        _fail(state, "build")
        opts, rest = _options(argv[1:])
        context = rest[0]
        files = sorted(str(p.relative_to(context)) for p in Path(context).rglob("*") if p.is_file())
        state.setdefault("builds", []).append(
            {"files": files, "containerfile": Path(opts["file"][0]).read_text(), "argv": argv}
        )
        image = hashlib.sha256(json.dumps(files).encode() + str(len(state["builds"])).encode())
        image_id = image.hexdigest()
        state["images"][image_id] = _labels(opts.get("label", []))
        # The base's entrypoint (``base_entrypoint``) is inherited unless the
        # Containerfile clears it; ``built_entrypoint`` is one a build sets.
        containerfile = Path(opts["file"][0]).read_text().splitlines()
        inherited = None if "ENTRYPOINT []" in containerfile else state.get("base_entrypoint")
        entrypoint = state.get("built_entrypoint") or inherited
        if entrypoint:
            state.setdefault("entrypoints", {})[image_id] = entrypoint
        _save(state)
        Path(opts["iidfile"][0]).write_text("sha256:" + image_id)
        return 0
    sys.stderr.write(f"fake runtime: unknown call {argv}\n")
    return 99


def _exec(state: dict, args: list[str]) -> int:
    opts, rest = _options(args)
    name, command = rest[0], rest[1:]
    container = state["containers"].get(name)
    if container is None:
        sys.stderr.write(f"Error: no container with name or ID {name} found\n")
        return 125
    if not container["running"]:
        sys.stderr.write(f"Error: container {name} is not running\n")
        return 125
    state.setdefault("execs", []).append({"container": name, "command": command, "opts": opts})
    _save(state)
    joined = " ".join(command)
    for word, answer in state.get("exec", {}).items():
        if word in command:
            sys.stdout.write(answer[1])
            return answer[0]
    if "receive" in command:
        data = sys.stdin.buffer.read()
        print(
            json.dumps(
                {
                    "stager": "received",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        )
        return 0
    if "pack" in command:
        sys.stdout.buffer.write(Path(state["pack_file"]).read_bytes())
        return 0
    if "/source/nonce" in command:
        if not state.get("shares_filesystem", True):
            sys.stderr.write("No such file or directory\n")
            return 1
        source = next(src for src, target in container["mounts"].items() if target == "/source")
        sys.stdout.write(Path(source, "nonce").read_text())
        return 0
    if "os.kill(-1" in joined:
        answer = state.get("teardown") or [0, json.dumps({"teardown": "proved", "survivors": 0})]
        if len(answer) > 2:
            import time

            time.sleep(answer[2])
        sys.stdout.write(answer[1])
        return answer[0]
    if command and command[-1] == "/home/agent/read-context":
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r") as tar:
            names = sorted(tar.getnames())
        state = _load()
        state.setdefault("read_contexts", []).append({"container": name, "names": names})
        _save(state)
        return 0
    # What the container was created with, as an exec inherits it; then the
    # exec's own names, valued from the client's environment.
    env = {n: v for n, v in container.get("env_values", {}).items() if v}
    env.update({n: os.environ[n] for n in opts.get("env", []) if n in os.environ})
    env["PATH"] = os.environ.get("PATH", "")
    if command and command[0] == "env":
        command = command[1:]
        while command and "=" in command[0]:
            key, _, value = command.pop(0).partition("=")
            env[key] = value
    return subprocess.run(command, env=env, stdin=sys.stdin).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


def install(bin_dir: Path, name: str, state: dict) -> Path:
    """This file as ``bin_dir/name``, with ``state`` beside it; the executable's path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    target.write_text(f"#!{sys.executable}\n" + Path(__file__).read_text())
    target.chmod(0o755)
    base = {"volumes": {}, "containers": {}, "images": {}}
    (bin_dir / "state.json").write_text(json.dumps({**base, **state}))
    return target


def calls(bin_dir: Path) -> list[dict]:
    path = bin_dir / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def state(bin_dir: Path) -> dict:
    return json.loads((bin_dir / "state.json").read_text())


def update(bin_dir: Path, **changes: object) -> None:
    current = state(bin_dir)
    current.update(changes)
    (bin_dir / "state.json").write_text(json.dumps(current))


PODMAN_VERSION = {"Client": {"APIVersion": "5.8.7", "Version": "5.8.7", "OsArch": "linux/amd64"}}


def podman_info(
    graph_root: str = "/home/op/.local/share/containers/storage", rootless: bool = True
) -> dict:
    return {
        "host": {"security": {"rootless": rootless}, "serviceIsRemote": False},
        "store": {
            "graphRoot": graph_root,
            "runRoot": "/run/user/1000/containers",
            "graphDriverName": "overlay",
        },
        "version": {"APIVersion": "5.8.7", "Version": "5.8.7"},
    }


DOCKER_VERSION = {
    "Client": {"Version": "27.3.1", "ApiVersion": "1.47"},
    "Server": {
        "Version": "27.3.1",
        "ApiVersion": "1.47",
        "Components": [{"Name": "Engine", "Version": "27.3.1"}],
    },
}


def docker_info(
    rootless: bool = True, engine_id: str = "ABCD:EFGH", root: str = "/home/op/.local/share/docker"
) -> dict:
    options = ["name=seccomp,profile=builtin", "name=cgroupns"]
    if rootless:
        options.append("name=rootless")
    return {
        "ID": engine_id,
        "DockerRootDir": root,
        "ServerVersion": "27.3.1",
        "OperatingSystem": "Ubuntu 24.04.1 LTS",
        "SecurityOptions": options,
    }
