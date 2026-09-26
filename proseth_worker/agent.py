"""The Proseth Worker agent.

Runs as a systemd service on an Ubuntu box inside a customer's network. It dials
out to the Supervisor, keeps one WebSocket open, and carries out the jobs it is
given from inside that network.

## The one thing to understand

**Nothing ever connects TO this machine.** The agent makes an outbound TCP
connection to the supervisor on port 9998 and everything travels down it. The
customer needs one outbound firewall rule and no inbound rule at all; the
supervisor needs no route into the customer's network and no VPN; and two
customers may use the same address space without colliding, because an address
is only ever resolved here.

## Reconnecting

The connection WILL drop - a link flaps, the supervisor restarts, a firewall
ages out an idle session. The agent reconnects with backoff, for ever, and says
so in the journal. A worker that gives up after three tries is a worker somebody
has to drive to a customer site to restart.

Jobs in flight when the connection drops are lost. The agent does not retry
them, and neither does the supervisor: half of these push configuration to a
switch or run terraform apply, and "it might have worked" is not a state to
resolve by doing it again.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import facts, protocol
from .jobs import EXECUTOR, HANDLERS

# The agent's version, and the ONE place it is written down. The installer
# reads it straight out of this file with sed, it goes up in the HELLO frame,
# and the Supervisor shows it on the Workers page - so "what is that site
# actually running" is answerable without logging into the box.
#
# Bump it whenever the agent or the installer changes in a way an existing
# worker should pick up. `sudo proseth-worker-update` is how a worker gets it.
VERSION = "1.2.1"

CONFIG_PATH = Path(os.environ.get("PROSETH_WORKER_CONFIG",
                                  "/etc/proseth-worker/config.json"))

log = logging.getLogger("proseth-worker")

# Reconnect backoff. Starts fast because most drops are a blip, and caps at a
# minute so a supervisor that is down for an hour is not hammered - while still
# recovering within a minute of it coming back.
BACKOFF_START = 2
BACKOFF_MAX = 60


def read_config() -> tuple[dict | None, str]:
    """The config, or a plain sentence saying why not.

    Split out from `load_config` so `--check` can report a problem WITHOUT the
    service's error logging and without exiting. The two callers want opposite
    things from the same failure: the service must log loudly and stop; the
    check is a human asking a question and should answer it.

    The distinction that matters is missing vs unreadable. The config is mode
    0640 root:proseth, so an ordinary user running `proseth-worker --check` -
    which the installer prints and the README documents - gets a permission
    error, and the old code reported that as "not set up yet", sending people
    to re-run the installer over a file that was perfectly fine.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle), ""
    except FileNotFoundError:
        return None, (f"no configuration at {CONFIG_PATH} - "
                      "run `sudo proseth-worker-setup`")
    except PermissionError:
        return None, (f"{CONFIG_PATH} is readable by root and the service "
                      "account only - run this with sudo to see it")
    except OSError as exc:
        return None, f"could not read {CONFIG_PATH}: {exc}"
    except ValueError as exc:
        return None, (f"{CONFIG_PATH} is not valid JSON ({exc}) - "
                      "run `sudo proseth-worker-setup`")


def load_config() -> dict:
    config, problem = read_config()
    if config is None:
        log.error("%s", problem[:1].upper() + problem[1:])
        raise SystemExit(78)
    return config


class Agent:
    def __init__(self, config: dict):
        self.host = str(config.get("supervisor_host") or "").strip()
        self.port = int(config.get("supervisor_port")
                        or protocol.DEFAULT_AGENT_PORT)
        self.token = str(config.get("token") or "").strip()
        self.name = str(config.get("worker_name") or "").strip()
        self.tls = bool(config.get("tls"))
        self.verify_tls = bool(config.get("verify_tls", True))
        # The Supervisor's certificate is self-signed, so it is not in this
        # machine's trust store and never will be. Being given a copy is what
        # lets verification actually mean something here - without it the only
        # way to use TLS at all is to turn verification off, which encrypts the
        # traffic and authenticates nobody.
        self.ca_cert = str(config.get("ca_cert") or "").strip()
        self.websocket = None
        self.stopping = asyncio.Event()
        # job id -> cancelled flag, so a handler can notice between steps.
        self.cancelled: set[str] = set()
        self.running: set[str] = set()
        # Set when the supervisor asked for a restart, so the exit path knows
        # to come back rather than stay down.
        self._restarting = False

        if not self.host or not self.token:
            log.error("The configuration is missing the supervisor address or "
                      "the token. Run: sudo proseth-worker-setup")
            raise SystemExit(78)

    @property
    def url(self) -> str:
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.host}:{self.port}/api/workers/gateway"

    # -- the loop -----------------------------------------------------------

    async def run(self) -> None:
        backoff = BACKOFF_START
        while not self.stopping.is_set():
            try:
                await self.session()
                # A clean return means the supervisor closed the socket. That is
                # usually a restart, so come back quickly.
                backoff = BACKOFF_START
            except _Denied as exc:
                # Wrong or revoked token, or a protocol mismatch. Retrying fast
                # achieves nothing and fills the journal; this needs a person.
                log.error("The supervisor refused this worker: %s", exc)
                log.error("Fix the token with `sudo proseth-worker-setup` or "
                          "re-issue it from the Workers page, then: "
                          "sudo systemctl restart proseth-worker")
                backoff = BACKOFF_MAX
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Disconnected from %s:%s (%s). Retrying in %ss.",
                            self.host, self.port, exc, backoff)
            if self.stopping.is_set():
                break
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(BACKOFF_MAX, backoff * 2)

    async def session(self) -> None:
        import websockets  # noqa: PLC0415

        # A `wss://` URI needs an SSL context, ALWAYS.
        #
        # This used to build one only when verification was being turned OFF,
        # which meant the ordinary, verifying TLS path passed `ssl=None` to a
        # `wss://` URI - and websockets refuses that outright:
        #
        #   ssl=None is incompatible with a wss:// URI
        #
        # The installer writes `verify_tls: true`, so that was the path every
        # normal install took: answering yes to TLS produced an agent that
        # could never connect and retried for ever with a message about a
        # keyword argument. The only combination that worked was the insecure
        # one. Verification is a property OF the context, not a reason to have
        # one.
        ssl_context = None
        if self.tls:
            import ssl  # noqa: PLC0415

            ssl_context = ssl.create_default_context()
            if self.ca_cert:
                # Trust THIS Supervisor's certificate and nothing else about
                # it. A self-signed certificate is not in the system store, so
                # without this the handshake fails with a bare
                # "certificate verify failed: self-signed certificate".
                try:
                    ssl_context.load_verify_locations(cafile=self.ca_cert)
                except OSError as exc:
                    log.error("Could not read the Supervisor certificate at "
                              "%s: %s", self.ca_cert, exc)
                    log.error("Run: sudo proseth-worker-setup")
                    raise SystemExit(78) from exc
            if not self.verify_tls:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        log.info("Connecting to %s", self.url)
        async with websockets.connect(
            self.url,
            ssl=ssl_context,
            # The supervisor pings every 20s. These keep an idle NAT or firewall
            # session from being aged out on a quiet night, which is the classic
            # way an agent looks online and is not.
            ping_interval=30,
            ping_timeout=30,
            close_timeout=5,
            max_size=32 * 1024 * 1024,
        ) as socket:
            self.websocket = socket
            await self.send(protocol.HELLO,
                            token=self.token,
                            version=VERSION,
                            protocol=protocol.PROTOCOL_VERSION,
                            name=self.name,
                            facts=facts.collect())

            raw = await asyncio.wait_for(socket.recv(), timeout=20)
            frame = protocol.parse(raw)
            if frame.get("t") == protocol.DENIED:
                raise _Denied(frame.get("reason") or "no reason given")
            if frame.get("t") != protocol.WELCOME:
                raise RuntimeError(f"Unexpected first frame: {frame.get('t')}")

            log.info("Connected as '%s' (worker %s)",
                     frame.get("name") or self.name, frame.get("worker_id"))
            interval = int(frame.get("heartbeat") or protocol.HEARTBEAT_SECONDS)
            beat = asyncio.create_task(self.heartbeat(interval))
            try:
                async for raw in socket:
                    await self.handle(protocol.parse(raw))
            finally:
                beat.cancel()
                self.websocket = None

    async def heartbeat(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            # Collected on a thread: reading /proc is quick but not free, and
            # the loop has pings to answer.
            payload = await asyncio.get_running_loop().run_in_executor(
                None, facts.collect)
            if not await self.send(protocol.HEARTBEAT, facts=payload):
                return

    # -- frames -------------------------------------------------------------

    async def send(self, kind: str, **fields) -> bool:
        socket = self.websocket
        if socket is None:
            return False
        try:
            await socket.send(protocol.frame(kind, **fields))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def handle(self, frame: dict) -> None:
        kind = frame.get("t")
        if kind == protocol.PING:
            await self.send(protocol.PONG, ts=frame.get("ts"))
        elif kind == protocol.JOB:
            asyncio.create_task(self.run_job(frame))
        elif kind == protocol.CANCEL:
            job_id = str(frame.get("job") or "")
            if job_id in self.running:
                log.info("Job %s cancelled from the supervisor", job_id)
                self.cancelled.add(job_id)
        elif kind == protocol.RELOAD:
            await self.send(protocol.HEARTBEAT, facts=facts.collect())
        elif kind == protocol.RESTART:
            # Acknowledged BEFORE exiting, so the supervisor learns the agent
            # accepted it rather than only that the connection dropped - which
            # is what a crash looks like too.
            reason = str(frame.get("reason") or "asked by the supervisor")
            log.info("Restart requested (%s) - exiting for the service "
                     "manager to bring us back", reason)
            await self.send(protocol.HEARTBEAT, facts=facts.collect(),
                            restarting=True)
            # Anything still running is abandoned deliberately: a restart is
            # what an engineer reaches for when a job is stuck, so waiting for
            # jobs to finish would defeat the entire point.
            self._restarting = True
            self.stop()
        elif kind == protocol.UPGRADE:
            await self.upgrade(frame)
        elif kind == protocol.DENIED:
            raise _Denied(frame.get("reason") or "no reason given")

    async def upgrade(self, frame: dict) -> None:
        """Fetch the current release and re-install, then come back.

        ## Why a wrapper and one sudoers line

        The agent runs as `proseth`, which has no sudo and cannot write to
        /opt - that is deliberate, so a compromised agent cannot rewrite its
        own code. Upgrading needs root, so there is exactly one thing the
        service account may run as root: `proseth-worker-selfupdate`, a
        root-owned wrapper installed by the installer. Nothing else is granted,
        and the wrapper takes no arguments, so there is no room to smuggle a
        command through it.

        ## Why it does not wait

        The wrapper hands the real work to a separate transient systemd unit
        and returns immediately. It has to: the installer restarts
        proseth-worker when it finishes, and anything running inside this
        service's own cgroup would be killed at that moment - half way through
        replacing the agent, which is the worst possible time to stop.

        So the acknowledgement here means "the upgrade has been started", not
        "the upgrade worked". What proves it worked is the agent reconnecting
        and reporting its new version, which the Supervisor already displays.
        """
        reason = str(frame.get("reason") or "asked by the supervisor")
        log.info("Upgrade requested (%s)", reason)

        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/local/bin/proseth-worker-selfupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            out = b"the upgrade launcher did not return within 60 seconds"

        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0:
            log.info("Upgrade started; the service will restart itself")
            await self.send(protocol.HEARTBEAT, facts=facts.collect(),
                            upgrading=True)
        else:
            # The likely cause is an agent installed before the sudoers rule
            # existed, so say the thing that fixes it rather than only the
            # exit code.
            log.error("Could not start the upgrade (exit %s): %s",
                      proc.returncode, text or "no output")
            log.error("If this says 'a password is required', this worker "
                      "predates one-click upgrade. Run once on the box: "
                      "sudo proseth-worker-update")
            await self.send(protocol.HEARTBEAT, facts=facts.collect(),
                            upgrade_error=(text or
                                           f"exit {proc.returncode}")[:400])

    async def run_job(self, frame: dict) -> None:
        job_id = str(frame.get("job") or "")
        kind = str(frame.get("kind") or "")
        payload = frame.get("payload") or {}
        handler = HANDLERS.get(kind)

        if handler is None:
            await self.send(protocol.RESULT, job=job_id, result={
                "ok": False,
                "error": f"This worker does not know the job kind '{kind}'. "
                         "It is probably older than the supervisor - re-run the "
                         "installer to update it.",
            })
            return

        self.running.add(job_id)
        loop = asyncio.get_running_loop()

        def emit(stream: str, text: str) -> None:
            # Called from the job's thread. `run_coroutine_threadsafe` is the
            # only safe way back onto the loop from there; touching the socket
            # directly would corrupt the WebSocket framing.
            try:
                asyncio.run_coroutine_threadsafe(
                    self.send(protocol.OUTPUT, job=job_id,
                              stream=stream, text=text),
                    loop,
                )
            except RuntimeError:
                pass  # the loop is shutting down

        def should_stop() -> bool:
            return job_id in self.cancelled or self.stopping.is_set()

        log.info("Job %s: %s", job_id, kind)
        try:
            result = await loop.run_in_executor(
                EXECUTOR, handler, payload, emit, should_stop)
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s raised", job_id)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self.running.discard(job_id)
            self.cancelled.discard(job_id)

        ok = bool((result or {}).get("ok"))
        log.info("Job %s finished: %s", job_id, "ok" if ok else "failed")
        await self.send(protocol.RESULT, job=job_id, result=result or {})

    def stop(self) -> None:
        """Shut down promptly on SIGTERM.

        Setting the event is not enough on its own: the session sits in
        `async for raw in socket`, which blocks until a frame arrives or the
        socket closes. With only the event set, systemd waited out its 90-second
        stop timeout and then SIGKILLed the agent on every restart - which shows
        up in the journal as `State 'stop-sigterm' timed out. Killing.` and
        makes an ordinary upgrade look like a crash.

        Closing the socket is what ends that loop.
        """
        self.stopping.set()
        socket = self.websocket
        if socket is not None:
            try:
                asyncio.create_task(socket.close())
            except RuntimeError:
                pass  # no running loop; nothing to close against


class _Denied(RuntimeError):
    """The supervisor refused us. Needs a person, not a retry."""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="proseth-worker",
        description="Proseth deploy agent. Connects out to the Supervisor.",
    )
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--check", action="store_true",
                        help="Show what this worker is and exit.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # No timestamp: journald adds one, and two is noise.
        format="%(levelname)-8s %(message)s",
    )
    if args.config:
        global CONFIG_PATH
        CONFIG_PATH = Path(args.config)

    if args.check:
        info = facts.collect()
        print(f"proseth-worker {VERSION}")
        print(f"  host    : {info.get('hostname')}")
        print(f"  distro  : {info.get('distro')}")
        print(f"  cpus    : {info.get('cpus')}   "
              f"memory: {info.get('memory_total_mb')} MB   "
              f"disk free: {info.get('disk_free_gb')} GB")
        print(f"  tools   : {', '.join(info.get('tools') or []) or 'none found'}")
        config, problem = read_config()
        if config is None:
            print(f"  config  : {problem}")
        else:
            print(f"  config  : {CONFIG_PATH}")
            print(f"  worker  : {config.get('worker_name')}")
            print(f"  connects: {config.get('supervisor_host')}:"
                  f"{config.get('supervisor_port')}"
                  + ("  (TLS)" if config.get("tls") else ""))
            print(f"  token   : {'set' if config.get('token') else 'MISSING'}")
        return 0

    agent = Agent(load_config())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except NotImplementedError:
            pass

    log.info("proseth-worker %s starting", VERSION)
    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        # `wait=False`: a restart is usually asked for BECAUSE a job is stuck,
        # so waiting for the pool would hang the very thing meant to unstick
        # it. systemd's TimeoutStopSec would then SIGKILL us anyway, just
        # slower and with a scarier journal entry.
        EXECUTOR.shutdown(wait=False)
        loop.close()

    if getattr(agent, "_restarting", False):
        log.info("proseth-worker exiting for restart")
        # A non-zero code so `Restart=on-failure` brings us back too, not only
        # `Restart=always`. os._exit rather than return: threads in the pool
        # may still be wedged - that is frequently why a restart was asked for
        # - and a normal interpreter exit would wait for them.
        os._exit(75)  # EX_TEMPFAIL

    log.info("proseth-worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
