"""Optional background-service installers: a systemd user unit (Linux) or a launchd agent (macOS).

`ftask serve` works fine in a terminal; these only keep it running across logins. Nothing here is needed to use ftask.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths
from .store import FtaskError, ValidationError

UNIT_NAME = "ftask.service"
LAUNCHD_LABEL = "ftask.serve"


def default_manager() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ValidationError("no service manager known for this platform; run `ftask serve` yourself")


def ftask_argv() -> list[str]:
    """Absolute command that starts ftask: the installed script if it is on PATH, else this interpreter -m ftask."""
    exe = shutil.which("ftask")
    if exe:
        return [os.path.abspath(exe)]
    return [sys.executable, "-m", "ftask"]


def serve_argv(host: str, port: int) -> list[str]:
    return ftask_argv() + ["serve", "--host", host, "--port", str(port)]


def _sd_quote(arg: str) -> str:
    if arg and not any(c in arg for c in ' \t"\'\\%$'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_systemd(argv: list[str], env: dict[str, str] | None = None) -> str:
    lines = [
        "[Unit]",
        "Description=ftask web UI (human-in-the-loop task inbox for AI agents)",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_sd_quote(a) for a in argv),
        "Restart=on-failure",
        "RestartSec=2",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    for k, v in (env or {}).items():
        lines.append("Environment=" + _sd_quote(f"{k}={v}"))
    lines += ["NoNewPrivileges=true", "PrivateTmp=true", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def render_launchd(argv: list[str], env: dict[str, str] | None = None, log: Path | None = None) -> str:
    log = log or (paths.data_dir() / "serve.log")
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **(env or {})},
    }
    return plistlib.dumps(plist).decode()


def unit_path(manager: str) -> Path:
    if manager == "systemd":
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / "systemd" / "user" / UNIT_NAME
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise FtaskError(f"{cmd[0]} not found; is this the right service manager? (see --manager)")
    if check and p.returncode != 0:
        raise FtaskError(f"{' '.join(cmd)} failed: {(p.stderr or p.stdout).strip()}")
    return p


def install(manager: str, host: str, port: int, print_only: bool, start: bool) -> int:
    env = {"FTASK_DB": os.environ["FTASK_DB"]} if os.environ.get("FTASK_DB") else {}
    argv = serve_argv(host, port)
    text = render_systemd(argv, env) if manager == "systemd" else render_launchd(argv, env)
    target = unit_path(manager)
    if print_only:
        print(f"# would write {target}")
        sys.stdout.write(text)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if manager == "launchd":
        paths.data_dir().mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print(f"wrote {target}")
    if not start:
        print("not started (--no-start)")
        return 0
    if manager == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
        _run(["systemctl", "--user", "restart", UNIT_NAME])
    else:
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        _run(["launchctl", "bootstrap", domain, str(target)])
    print(f"running: http://{host}:{port}")
    return 0


def uninstall(manager: str) -> int:
    target = unit_path(manager)
    if manager == "systemd":
        _run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    else:
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
    if target.exists():
        target.unlink()
        print(f"removed {target}")
    else:
        print(f"nothing to remove at {target}")
    if manager == "systemd":
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    return 0


def status(manager: str) -> int:
    target = unit_path(manager)
    print(f"{target}: {'installed' if target.exists() else 'not installed'}")
    if manager == "systemd":
        p = _run(["systemctl", "--user", "is-active", UNIT_NAME], check=False)
    else:
        p = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        print("loaded" if p.returncode == 0 else "not loaded")
        return 0 if p.returncode == 0 else 3
    print(p.stdout.strip() or p.stderr.strip())
    return 0 if p.returncode == 0 else 3
