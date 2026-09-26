"""What a worker actually does. Every one of these runs inside the customer's network.

The supervisor decides WHAT should happen; this decides nothing. It receives a
job, carries it out against machines only it can reach, and streams the output
back. That split is the whole architecture: the brain has no route to the
customer, and the hands have no opinions.

## Rules every handler follows

**Stream, do not collect.** A terraform apply or an Ansible run is minutes long.
Lines go back as they are produced, so the engineer watching the log sees a
build happening rather than a blank screen they assume has hung.

**Never log a secret.** Credentials arrive in the job payload, are used, and are
not echoed. An SSH password goes to paramiko, never to `emit`.

**Never invent a target.** If a payload is missing the address or the
credential, the job fails saying so. Guessing produces a config pushed to the
wrong box.

**A cancelled job stops.** `should_stop()` is checked between steps, so a
runaway run does not carry on against a customer's kit with nobody watching.
"""
from __future__ import annotations

import io
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

# `emit(stream, text)` - one line of output, as it happens.
Emit = Callable[[str, str], None]
ShouldStop = Callable[[], bool]

# Jobs run on threads: every one of them is blocking (paramiko, subprocess), and
# the agent's event loop has to stay free to answer pings and accept a cancel.
# Four at once is plenty for one site and keeps a runaway job from starving the
# heartbeat.
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job")


def _run_streaming(
    argv: list[str],
    emit: Emit,
    should_stop: ShouldStop,
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: int = 7200,
    label: str = "",
) -> tuple[int, str]:
    """Run a command, streaming its output line by line.

    stderr is folded into stdout deliberately. Ansible and terraform both write
    useful progress to stderr, and two interleaved streams reconstructed at the
    far end arrive out of order - which makes a failure look like it happened
    somewhere it did not.
    """
    if label:
        emit("stdout", f"$ {label}")
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        return 127, f"'{argv[0]}' is not installed on this worker."
    except OSError as exc:
        return 1, f"Could not start '{argv[0]}': {exc}"

    started = time.monotonic()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            emit("stdout", line.rstrip("\r\n"))
            if should_stop():
                proc.kill()
                return 130, "Cancelled from the supervisor."
            if time.monotonic() - started > timeout:
                proc.kill()
                return 124, f"Timed out after {timeout}s."
        proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode or 0, ""


# --------------------------------------------------------------------- shell


def run_shell(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """A bash script, on the worker itself.

    Used by anything that needs a foothold inside the customer's network without
    a specific target - a discovery script, a one-off check.
    """
    script = payload.get("script") or ""
    if not script.strip():
        return {"ok": False, "error": "No script was supplied."}

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        os.chmod(path, 0o700)
        code, error = _run_streaming(
            ["/bin/bash", path], emit, should_stop,
            timeout=int(payload.get("timeout") or 3600),
        )
        return {"ok": code == 0, "exit_code": code,
                "error": error or ("" if code == 0 else f"Exited with {code}.")}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ----------------------------------------------------------------------- ssh


def _ssh_client(target: dict):
    """A connected paramiko client for one host, or a readable failure."""
    import paramiko  # noqa: PLC0415

    address = str(target.get("address") or "").strip()
    if not address:
        raise ValueError("The job did not say which host to connect to.")

    pkey = None
    key_text = target.get("private_key")
    if key_text:
        last: Exception | None = None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                pkey = cls.from_private_key(
                    io.StringIO(key_text), password=target.get("passphrase") or None)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if pkey is None:
            raise ValueError(f"That private key could not be read ({last}).")

    client = paramiko.SSHClient()
    # Trust on first use, same as the supervisor did before workers existed. A
    # customer's management LAN where nobody has recorded host keys would
    # otherwise refuse every connection. It does not protect against an on-path
    # attacker - but the worker is INSIDE that network, which is a materially
    # better position than reaching across the internet to it.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=address,
        port=int(target.get("port") or 22),
        username=target.get("username") or "",
        password=target.get("password") or None,
        pkey=pkey,
        timeout=20, banner_timeout=20, auth_timeout=20,
        look_for_keys=False, allow_agent=False,
    )
    return client


def run_ssh(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Pipe a script into bash on a remote host and stream the result.

    Piped rather than uploaded: nothing is left behind on the customer's box,
    and there is no temp file to collide or leak. The same approach the
    supervisor used before, moved to where the route exists.
    """
    import paramiko  # noqa: PLC0415

    target = payload.get("target") or {}
    script = payload.get("script") or ""
    if not script.strip():
        return {"ok": False, "error": "No script was supplied."}

    captures: dict[str, str] = {}
    try:
        client = _ssh_client(target)
    except paramiko.AuthenticationException:
        return {"ok": False, "error": _auth_hint(target)}
    except (paramiko.SSHException, OSError) as exc:
        return {"ok": False, "error":
                f"Could not reach {target.get('address')} from this worker: {exc}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    timeout = int(payload.get("timeout") or 3600)
    try:
        chan = client.get_transport().open_session()
        chan.settimeout(timeout)
        chan.set_combine_stderr(True)
        chan.exec_command("bash -s")
        chan.sendall(script.encode("utf-8"))
        chan.shutdown_write()

        # The deadline is enforced HERE, in the loop, not by `settimeout`.
        #
        # `settimeout` only bounds a BLOCKING read. Every call below is a
        # non-blocking poll - `recv_ready`, `exit_status_ready` - so a command
        # that neither produces output nor exits spins this loop for ever and
        # the channel timeout never fires. That is not theoretical: three
        # wedged `ssh` jobs filled the four-thread executor on the live worker
        # and every new job queued behind them for ever, while the agent went
        # on answering heartbeats and reporting itself perfectly healthy.
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            if should_stop():
                chan.close()
                return {"ok": False, "error": "Cancelled from the supervisor."}
            if time.monotonic() > deadline:
                chan.close()
                return {
                    "ok": False,
                    "error": f"No output and no exit after {timeout}s - the "
                             "command on the target is still running, so it "
                             "was abandoned. Check it by hand before running "
                             "this again.",
                }
            if chan.recv_ready():
                chunk = chan.recv(8192)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if not _harvest(line, captures):
                        emit("stdout", line)
            elif chan.exit_status_ready():
                break
            else:
                time.sleep(0.05)

        while chan.recv_ready():
            buf += chan.recv(8192)
        for raw in buf.split(b"\n"):
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line and not _harvest(line, captures):
                emit("stdout", line)

        code = chan.recv_exit_status()
        return {
            "ok": code == 0,
            "exit_code": code,
            "captures": captures,
            "error": "" if code == 0 else _exit_hint(code),
        }
    finally:
        client.close()


# A phase announces a value for later phases by printing this. Kept identical to
# the supervisor's own marker so the existing blueprint scripts work unchanged
# whether they run here or there.
CAPTURE_PREFIX = "PROSETH_CAPTURE:"


def _harvest(line: str, captures: dict) -> bool:
    stripped = line.strip()
    if not stripped.startswith(CAPTURE_PREFIX):
        return False
    name, _, value = stripped[len(CAPTURE_PREFIX):].partition("=")
    if name.strip():
        captures[name.strip()] = value.strip()
    return True


def _auth_hint(target: dict) -> str:
    who = target.get("username") or "that account"
    where = target.get("address")
    if target.get("private_key"):
        return (f"{where} refused the key for '{who}'. Check the public half is "
                "in that user's authorized_keys.")
    return (f"{where} refused the password for '{who}'. Check the username, and "
            "that sshd allows password authentication.")


def _exit_hint(code: int) -> str:
    # 78 is EX_CONFIG - the blueprint scripts use it for "this host is not in a
    # state where the job can even be attempted", which is different from a step
    # that tried and failed.
    if code == 78:
        return ("The host did not meet the requirements - see the log above for "
                "what was missing.")
    return f"The script exited with status {code}."


LINUX_FACTS = r"""
echo "hostname=$(hostname -f 2>/dev/null || hostname)"
echo "kernel=$(uname -r)"
echo "arch=$(uname -m)"
. /etc/os-release 2>/dev/null && echo "distro=$NAME" && echo "version=$VERSION_ID"
echo "cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
echo "memory_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)"
echo "disk_free_gb=$(df -BG / 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')"
echo "python=$(command -v python3 || command -v python || echo none)"
echo "docker=$(command -v docker >/dev/null 2>&1 && echo yes || echo no)"
echo "systemd=$(command -v systemctl >/dev/null 2>&1 && echo yes || echo no)"
echo "sudo_nopasswd=$(sudo -n true 2>/dev/null && echo yes || echo no)"
# Which sudo this host has, and one Ansible can actually drive.
#
# Ubuntu 25.10 and later ship `sudo-rs` as the default `sudo`, and Ansible's
# become plugin cannot detect its password prompt - every play dies with
# "Timeout (12s) waiting for privilege escalation prompt", which reads like a
# network problem and is not. Classic sudo is still installed alongside it as
# /usr/bin/sudo.ws, and pointing Ansible at that works. Reported here so the
# inventory can set ansible_become_exe without anybody having to know this.
SUDO_V="$(sudo --version 2>/dev/null | head -1)"
echo "sudo_version=$SUDO_V"
case "$SUDO_V" in
  *sudo-rs*)
    echo "sudo_impl=sudo-rs"
    [ -x /usr/bin/sudo.ws ] && echo "sudo_exe=/usr/bin/sudo.ws"
    ;;
  *) echo "sudo_impl=classic" ;;
esac
"""


def run_probe(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Prove a host is reachable from THIS worker, and record what it is.

    The reachability half matters more than it used to. Before workers, "can the
    platform reach this host" and "can we deploy to it" were the same question.
    Now they are not, and this is the one that counts.

    The facts script writes plain `key=value` lines to stdout rather than using
    the PROSETH_CAPTURE marker, so they have to be collected HERE as they stream
    past. The first version read `result["captures"]` and always got an empty
    dict - the probe reported success with no facts at all, which looks like a
    reachable host that somehow has no CPU or memory.
    """
    target = payload.get("target") or {}
    emit("stdout", f"Reaching {target.get('address')} from this worker...")

    collected: list[str] = []

    def tee(stream: str, text: str) -> None:
        collected.append(text)
        emit(stream, text)

    result = run_ssh({**payload, "script": LINUX_FACTS}, tee, should_stop)
    if not result.get("ok"):
        return result

    facts: dict = {}
    for line in collected:
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key and value and " " not in key:
            facts[key] = value
    for key in ("cpus", "memory_mb", "disk_free_gb"):
        if key in facts:
            try:
                facts[key] = int(facts[key])
            except ValueError:
                pass
    return {**result, "facts": facts}


# ------------------------------------------------------------------- ansible


def run_ansible(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Run a playbook, with the worker as the control node.

    This is what retires the old "nominate one of the customer's Linux boxes as
    an Ansible control node" workaround: the worker already IS a Linux box
    inside the customer's network, and it is one we installed and control.

    The inventory carries the target machines' credentials, so it is written
    0600 inside a 0700 directory and removed in a `finally` - the same handling
    the supervisor's version used, for the same reason.
    """
    playbook = payload.get("playbook") or ""
    inventory = payload.get("inventory") or ""
    if not playbook.strip():
        return {"ok": False, "error": "No playbook was supplied."}
    if not shutil.which("ansible-playbook"):
        return {"ok": False, "error":
                "ansible-playbook is not installed on this worker. Re-run the "
                "installer, or `sudo apt install ansible`."}

    workdir = tempfile.mkdtemp(prefix="proseth-ansible-")
    try:
        os.chmod(workdir, 0o700)
        play_path = os.path.join(workdir, "playbook.yml")
        with open(play_path, "w", encoding="utf-8") as handle:
            handle.write(playbook)

        argv = ["ansible-playbook", play_path]
        if inventory:
            inv_path = os.path.join(workdir, "inventory.ini")
            with open(inv_path, "w", encoding="utf-8") as handle:
                handle.write(inventory)
            os.chmod(inv_path, 0o600)
            argv += ["-i", inv_path]

        # SSH keys the inventory refers to as `keys/<id>.pem`, relative to the
        # working directory. Flat names only: a path here would be a way to
        # write anywhere on the worker's filesystem, and a key file has no
        # reason to need one.
        keys = payload.get("keys") or {}
        if keys:
            key_dir = os.path.join(workdir, "keys")
            os.makedirs(key_dir, mode=0o700, exist_ok=True)
            for name, body in keys.items():
                safe = os.path.basename(str(name))
                if not safe or safe.startswith("."):
                    continue
                key_path = os.path.join(key_dir, safe)
                with open(key_path, "w", encoding="utf-8") as handle:
                    handle.write(body)
                # OpenSSH refuses a key any wider than this, and says so in a
                # message that does not mention permissions.
                os.chmod(key_path, 0o600)

        # Ansible writes control-path sockets, a fact cache and a temp tree into
        # $HOME before it does anything else, and refuses to start if it cannot.
        # The agent's HOME is /opt/proseth-worker, which is root-owned so the
        # service account cannot write to it - deliberately, since that is where
        # the agent's own code lives. So Ansible is pointed at the state
        # directory instead, explicitly rather than relying on HOME being right.
        state = os.environ.get("PROSETH_STATE_DIR") or os.path.expanduser("~")
        ansible_home = os.path.join(state, ".ansible")
        os.makedirs(os.path.join(ansible_home, "tmp"), mode=0o700, exist_ok=True)

        env = {
            **os.environ,
            "HOME": state,
            "ANSIBLE_HOME": ansible_home,
            "ANSIBLE_LOCAL_TEMP": os.path.join(ansible_home, "tmp"),
            # The customer's hosts are almost never in known_hosts on a freshly
            # built worker, and a playbook that stops to ask is a playbook that
            # hangs for ever with nobody at the keyboard.
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_FORCE_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }
        # Which sudo to drive. See the note in LINUX_FACTS: on Ubuntu 25.10+
        # the default `sudo` is sudo-rs, whose prompt Ansible cannot detect, and
        # every play fails with a 12-second privilege-escalation timeout that
        # looks nothing like the actual cause. The probe records the classic
        # binary's path; passing it here is what makes become work.
        become_exe = str(payload.get("become_exe") or "").strip()
        if become_exe:
            env["ANSIBLE_BECOME_EXE"] = become_exe
            emit("stdout", f"using {become_exe} for privilege escalation")
        code, error = _run_streaming(
            argv, emit, should_stop, cwd=workdir, env=env,
            timeout=int(payload.get("timeout") or 7200),
            label="ansible-playbook playbook.yml",
        )
        return {"ok": code == 0, "exit_code": code,
                "error": error or ("" if code == 0 else
                                   f"ansible-playbook exited with {code}.")}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------------------------------------------- terraform


def run_terraform(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """terraform in a directory on this worker.

    **The state file lives here**, which is the thing to understand about
    running terraform from a worker. State is the only record of what exists in
    the customer's cloud account; lose it and the stack can never be destroyed
    or updated again. The supervisor pulls it back after every run - see the
    `state` field in the result - and that sync is not optional.
    """
    action = str(payload.get("action") or "plan")
    if action not in ("init", "plan", "apply", "destroy", "output"):
        return {"ok": False, "error": f"Unknown terraform action '{action}'."}
    if not shutil.which("terraform"):
        return {"ok": False, "error":
                "terraform is not installed on this worker. Re-run the installer."}

    workdir = payload.get("workdir") or os.path.expanduser(
        f"~/.proseth/terraform/{payload.get('stack') or 'default'}")
    os.makedirs(workdir, mode=0o700, exist_ok=True)

    for name, body in (payload.get("files") or {}).items():
        # Flat filenames only. A path in a job payload is a way to write
        # anywhere on the worker's filesystem, and there is no reason for a
        # terraform file to need one.
        safe = os.path.basename(str(name))
        if not safe or safe.startswith("."):
            continue
        with open(os.path.join(workdir, safe), "w", encoding="utf-8") as handle:
            handle.write(body)

    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    # Cloud credentials arrive per job and live only in this process's memory
    # for the length of it. They are never written to the working directory.
    for key, value in (payload.get("env") or {}).items():
        env[str(key)] = str(value)

    commands = {
        "init": ["terraform", "init", "-no-color", "-input=false"],
        "plan": ["terraform", "plan", "-no-color", "-input=false",
                 "-out=tfplan"],
        "apply": ["terraform", "apply", "-no-color", "-input=false",
                  "-auto-approve", "tfplan"],
        "destroy": ["terraform", "destroy", "-no-color", "-input=false",
                    "-auto-approve"],
        "output": ["terraform", "output", "-json", "-no-color"],
    }
    code, error = _run_streaming(
        commands[action], emit, should_stop, cwd=workdir, env=env,
        timeout=int(payload.get("timeout") or 7200),
        label=" ".join(commands[action]),
    )

    result = {"ok": code == 0, "exit_code": code, "workdir": workdir,
              "error": error or ("" if code == 0 else
                                 f"terraform {action} exited with {code}.")}

    # Hand the state back so the supervisor holds the authoritative copy. It
    # contains secrets (VPN pre-shared keys, generated passwords), which is why
    # the supervisor encrypts it - but losing it is worse than holding it.
    if payload.get("return_state"):
        state_path = os.path.join(workdir, "terraform.tfstate")
        try:
            with open(state_path, encoding="utf-8") as handle:
                result["state"] = handle.read()
        except OSError:
            result["state"] = ""
    return result


# ------------------------------------------------------------------- netmiko


def run_netmiko(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Push switch CLI over SSH, from inside the customer's network.

    The reason VXLAN and BGP deploys can reach customer hardware at all: the
    management addresses on those switches are private, frequently overlap
    between customers, and were never routable from the supervisor.
    """
    try:
        from netmiko import ConnectHandler  # noqa: PLC0415
    except ImportError:
        return {"ok": False, "error":
                "netmiko is not installed on this worker. Re-run the installer."}

    device = payload.get("device") or {}
    commands = payload.get("commands") or []
    # `show` mode is how a preflight reaches a switch through a worker: read the
    # version banner, send nothing. Kept as a mode rather than a second handler
    # because the connection, the error messages and the hint are identical -
    # and a preflight that connected differently from the apply would not be
    # proving anything about the apply.
    mode = str(payload.get("mode") or "config")
    # `commit` (1.2.1): Junos and PAN-OS keep changes in a candidate
    # configuration until committed. A commit can run for minutes, so it is
    # Netmiko's commit() - which waits for the vendor's completion marker and
    # raises without it - not a line inside a config set.
    if mode not in ("config", "show", "commit"):
        return {"ok": False, "error": f"Unknown netmiko mode '{mode}'."}
    if not commands:
        return {"ok": False, "error": "No commands were supplied."}

    params = {
        "device_type": device.get("driver") or "cisco_ios",
        "host": device.get("address"),
        "port": int(device.get("port") or 22),
        "username": device.get("username") or "",
        "password": device.get("password") or "",
        "secret": device.get("enable") or "",
        "conn_timeout": 30,
        "fast_cli": False,
    }
    if not params["host"]:
        return {"ok": False, "error": "The job did not say which device to reach."}

    emit("stdout", f"Connecting to {params['host']}:{params['port']} "
                   f"as {params['device_type']}")
    try:
        with ConnectHandler(**params) as conn:
            if params["secret"]:
                conn.enable()
            prompt = conn.find_prompt()
            emit("stdout", f"Connected: {prompt}")
            if should_stop():
                return {"ok": False, "error": "Cancelled before anything was sent."}

            if mode == "show":
                chunks = []
                for command in commands:
                    text = conn.send_command(command, read_timeout=120)
                    chunks.append(text or "")
                    for line in (text or "").splitlines():
                        emit("stdout", line)
                return {"ok": True, "output": "\n".join(chunks), "prompt": prompt}

            if mode == "commit":
                words = " ".join(commands).split()
                kwargs: dict = {}
                if words[:1] == ["confirmed"]:
                    kwargs["confirm"] = True
                    if len(words) > 1 and words[1].isdigit():
                        kwargs["confirm_delay"] = int(words[1])
                emit("stdout", "Committing" + (f" ({' '.join(words)})" if words else ""))
                try:
                    output = conn.commit(read_timeout=900, **kwargs)
                except ValueError as exc:
                    return {"ok": False, "error": f"The commit was refused: {exc}"[:2000]}
                for line in (output or "").splitlines():
                    emit("stdout", line)
                if conn.check_config_mode():
                    conn.exit_config_mode()
                return {"ok": True, "output": output, "prompt": prompt}

            output = conn.send_config_set(
                commands, cmd_verify=False, read_timeout=120)
            for line in output.splitlines():
                emit("stdout", line)

            saved = ""
            if payload.get("save"):
                emit("stdout", "Saving the configuration")
                saved = conn.save_config()
                for line in saved.splitlines():
                    emit("stdout", line)
            return {"ok": True, "output": output, "saved": saved, "prompt": prompt}
    except Exception as exc:  # noqa: BLE001
        # Netmiko's exception names are more useful to an engineer than the
        # message alone, which is often just a timeout.
        return {"ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "Check the address is reachable FROM THIS WORKER, that "
                        "the credentials are right, and that the platform "
                        "matches the device."}


# ------------------------------------------------------------------ discover


def run_discover(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Find what answers on a port across a range, from inside the network.

    The point is not a security scan - it is that an engineer standing up a new
    customer should not have to type twenty addresses that only exist inside
    that customer's network. Point the worker at the management subnet and it
    reports what is there.

    TCP connect only, on one port. Nothing is fingerprinted and nothing is
    logged into.
    """
    import ipaddress  # noqa: PLC0415

    cidr = str(payload.get("cidr") or "").strip()
    port = int(payload.get("port") or 22)
    timeout = float(payload.get("timeout") or 1.0)
    if not cidr:
        return {"ok": False, "error": "No range was supplied."}

    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        return {"ok": False, "error": f"'{cidr}' is not a valid range: {exc}"}

    # A /16 is 65534 connects. Refused rather than attempted: it would take
    # hours, hammer the customer's network, and is almost always a typo for a
    # /24.
    if network.num_addresses > 1024:
        return {"ok": False, "error":
                f"{cidr} is {network.num_addresses} addresses. Scan a /22 or "
                "smaller - anything larger is usually a mistyped prefix."}

    hosts = list(network.hosts()) or [network.network_address]
    emit("stdout", f"Checking {len(hosts)} address(es) on port {port} "
                   f"from this worker")
    found: list[dict] = []

    def probe(ip: str) -> tuple[str, bool]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            return ip, sock.connect_ex((ip, port)) == 0
        except OSError:
            return ip, False
        finally:
            sock.close()

    with ThreadPoolExecutor(max_workers=64) as pool:
        for ip, ok in pool.map(probe, [str(h) for h in hosts]):
            if should_stop():
                return {"ok": False, "error": "Cancelled from the supervisor.",
                        "found": found}
            if ok:
                emit("stdout", f"  {ip}:{port} answered")
                found.append({"address": ip, "port": port})

    emit("stdout", f"{len(found)} host(s) answered on port {port}")
    return {"ok": True, "found": found, "scanned": len(hosts)}


# ------------------------------------------------------------------- winrm
#
# Windows behind a worker. The Supervisor reaches Windows with its own
# PowerShell remoting, which a Linux worker does not have - and before this job
# existed a Windows host behind a worker was sent down the SSH path, which
# connected to WinRM's port and reported "Error reading SSH protocol banner".
#
# pywinrm is what Ansible uses for the same job. Two decisions worth knowing:
#
# **The script travels on STDIN, base64, not on the command line.** A command
# line has a length limit the hardening script alone exceeds several times
# over, and base64 is plain ASCII so the shell's code page cannot mangle it.
# The command itself is a fixed, tiny bootstrap that reads stdin, decodes it
# and runs it as a script block - the same thing the Supervisor's own runner
# does with an environment variable.
#
# **NTLM with message encryption, over HTTP on 5985 by default.** That works
# for local and domain accounts with no Kerberos set-up on the worker, and
# pywinrm encrypts the payload itself - the credential and the script are not
# in clear on the customer's LAN. 5986 means HTTPS; its certificate is almost
# always self-signed, so it is not validated, which is the same trust-on-first-
# use position the SSH jobs take with host keys.

_PS_BOOTSTRAP = (
    "$ErrorActionPreference='Stop';"
    "$b=[Console]::In.ReadToEnd();"
    "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b.Trim()));"
    "& ([ScriptBlock]::Create($s))"
)


def _winrm_session(target: dict):
    """A pywinrm Protocol and the account name it will log in as."""
    try:
        from winrm.protocol import Protocol  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pywinrm is not installed on this worker, so it cannot reach Windows. "
            "Update the worker (Upgrade on the Workers page, or "
            "`sudo proseth-worker-update`)."
        ) from exc

    address = str(target.get("address") or "").strip()
    if not address:
        raise ValueError("The job did not say which host to connect to.")
    if not target.get("password"):
        raise ValueError("Windows hosts need a password - WinRM has no key login.")

    port = int(target.get("port") or 5985)
    https = port == 5986 or bool(target.get("https"))
    user = str(target.get("username") or "")
    domain = str(target.get("domain") or "")
    if domain and "\\" not in user and "@" not in user:
        user = f"{domain}\\{user}"

    endpoint = f"{'https' if https else 'http'}://{address}:{port}/wsman"
    proto = Protocol(
        endpoint=endpoint,
        transport="ntlm",
        username=user,
        password=target.get("password"),
        server_cert_validation="ignore",
        message_encryption="auto",
        read_timeout_sec=70,
        operation_timeout_sec=60,
    )
    return proto, user


def _clixml_errors(raw: str) -> list[str]:
    """PowerShell sends errors on stderr as CLIXML. Pull out the readable text."""
    import re  # noqa: PLC0415

    if "#< CLIXML" not in raw:
        return [line for line in raw.splitlines() if line.strip()]
    out = []
    for m in re.finditer(r'<S S="Error">(.*?)</S>', raw, re.S):
        text = (m.group(1).replace("_x000D_", "").replace("_x000A_", "\n")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&apos;", "'"))
        out.extend(t for t in text.splitlines() if t.strip())
    return out


def _winrm_hint(message: str, target: dict) -> str:
    low = message.lower()
    where = target.get("address")
    if "401" in low or "unauthorized" in low or "credentials were rejected" in low:
        return (f"{where} refused the login for '{target.get('username')}'. Check "
                "the password, and the domain if it is a domain account.")
    if "connection refused" in low or "max retries" in low or "timed out" in low \
            or "no route" in low:
        return (f"Could not reach WinRM on {where}:{target.get('port') or 5985} from "
                "this worker. On the server: `Enable-PSRemoting -Force`, and allow "
                "TCP 5985 (HTTP) or 5986 (HTTPS) from the worker in its firewall.")
    if "access is denied" in low or "access denied" in low:
        return (f"{where} accepted the login but refused the command. The account "
                "must be a local Administrator or in Remote Management Users.")
    return f"{where}: {message}"


def run_winrm(payload: dict, emit: Emit, should_stop: ShouldStop) -> dict:
    """Run a PowerShell script on a Windows host, streaming its output."""
    import base64  # noqa: PLC0415

    target = payload.get("target") or {}
    script = payload.get("script") or ""
    if not script.strip():
        return {"ok": False, "error": "No script was supplied."}
    timeout = int(payload.get("timeout") or 3600)

    try:
        proto, _ = _winrm_session(target)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    captures: dict[str, str] = {}
    shell_id = command_id = None
    try:
        # 65001 is UTF-8, so a non-English server's output survives the trip.
        shell_id = proto.open_shell(codepage=65001, noprofile=True)
        command_id = proto.run_command(
            shell_id, "powershell.exe",
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand",
             base64.b64encode(_PS_BOOTSTRAP.encode("utf-16-le")).decode("ascii")],
        )
        body = base64.b64encode(script.encode("utf-8"))
        chunk = 96 * 1024  # well inside WinRM's default 500 KB envelope
        for i in range(0, len(body), chunk):
            proto.send_command_input(shell_id, command_id, body[i:i + chunk],
                                     end=i + chunk >= len(body))

        # get_command_output_raw in pywinrm 0.5, _raw_get_command_output before.
        poll = getattr(proto, "get_command_output_raw", None) or \
            getattr(proto, "_raw_get_command_output")
        deadline = time.monotonic() + timeout
        buf, errbuf, code, done = "", "", 0, False
        while not done:
            if should_stop():
                return {"ok": False, "error": "Cancelled from the supervisor."}
            if time.monotonic() > deadline:
                return {"ok": False, "error": f"No exit after {timeout}s - abandoned. "
                        "Check the machine by hand before running this again."}
            try:
                out, err, code, done = poll(shell_id, command_id)
            except Exception as exc:  # noqa: BLE001
                # An operation timeout means "nothing new yet", not a failure -
                # WinRM long-polls, and a quiet step is normal.
                if "OperationTimeout" in type(exc).__name__ or "2150858793" in str(exc):
                    continue
                raise
            buf += out.decode("utf-8", "replace")
            errbuf += err.decode("utf-8", "replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                line = line.rstrip("\r")
                if not _harvest(line, captures):
                    emit("stdout", line)
        for line in buf.splitlines():
            if line.strip() and not _harvest(line, captures):
                emit("stdout", line)
        errors = _clixml_errors(errbuf)
        for line in errors[:40]:
            emit("stdout", f"!! {line}")
        ok = code == 0
        return {
            "ok": ok,
            "exit_code": code,
            "captures": captures,
            "error": "" if ok else (
                _exit_hint(code) if not errors else
                f"{errors[0][:300]} (exit {code})"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _winrm_hint(f"{type(exc).__name__}: {exc}", target)}
    finally:
        try:
            if shell_id and command_id:
                proto.cleanup_command(shell_id, command_id)
            if shell_id:
                proto.close_shell(shell_id)
        except Exception:  # noqa: BLE001
            pass


HANDLERS = {
    "shell": run_shell,
    "ssh": run_ssh,
    "probe": run_probe,
    "ansible": run_ansible,
    "terraform": run_terraform,
    "netmiko": run_netmiko,
    "discover": run_discover,
    "winrm": run_winrm,
}
