"""The wire protocol between the Supervisor and a Worker.

**This file is copied verbatim into the agent.** It is the one piece of code that
exists on both sides, so it deliberately imports nothing but the standard
library - the worker is a bare Ubuntu box with Python and no FastAPI, and
anything imported here has to exist there too.

## The shape, and why it is this shape

The worker **dials out**. It opens a WebSocket to the supervisor and keeps it
open; every job travels down that connection. The supervisor never initiates
anything towards a customer network.

That is not a stylistic choice. It removes, in one decision: inbound firewall
rules at every customer, a VPN per site, routing from this server into customer
management networks, and - the one that actually bites - overlapping address
space. Three customers can each own 192.168.1.10 and none of them collide,
because an address is only ever resolved by the worker living in that network.

## Frames

Every frame is one JSON object with a `t` (type) field. Small, boring, and
readable in a log, which matters more than compactness at this volume.

Worker -> Supervisor                  Supervisor -> Worker
  hello      authenticate              welcome    accepted, here is your config
  heartbeat  health + facts            ping       latency probe
  pong       latency reply             job        do this
  output     a line of job output      cancel     stop that job
  result     job finished              reload     re-send your facts

## Two rules worth keeping

**Output is streamed, not collected.** A terraform apply or an Ansible run is
minutes long, and an engineer watching a blank screen assumes it has hung. Lines
go back as they happen and land in the same SSE stream the UI already uses.

**A job id is chosen by the supervisor.** The worker never invents one, so a
duplicate delivery after a reconnect is detectable rather than silently running
the job twice.
"""
from __future__ import annotations

import json
from typing import Any

# Bumped when a frame changes shape in a way an old agent could not handle. The
# supervisor refuses a worker whose major version it does not know, rather than
# letting it connect and fail confusingly on the first job.
PROTOCOL_VERSION = 1

# The port the agent dials. Deliberately NOT the UI port: only this one needs to
# be reachable from customer sites, which makes the firewall request a customer
# has to approve a single line.
DEFAULT_AGENT_PORT = 9998

# How often the worker reports health. Also the liveness signal - a worker that
# stops sending these is marked offline after MISSED_HEARTBEATS of them.
HEARTBEAT_SECONDS = 20
MISSED_HEARTBEATS = 3

# --- frame types -----------------------------------------------------------

# worker -> supervisor
HELLO = "hello"
HEARTBEAT = "heartbeat"
PONG = "pong"
OUTPUT = "output"
RESULT = "result"

# supervisor -> worker
WELCOME = "welcome"
PING = "ping"
JOB = "job"
CANCEL = "cancel"
RELOAD = "reload"
DENIED = "denied"

# --- job kinds -------------------------------------------------------------
#
# What a worker is actually asked to do. Every one of these runs INSIDE the
# customer's network, which is the whole point.

# Run a shell script. The workhorse - blueprints, ad-hoc tasks, probes.
JOB_SHELL = "shell"
# Reach a host over SSH and run a script there. The worker is the SSH client.
JOB_SSH = "ssh"
# Collect facts from a host (distro, cpus, memory, sudo) - the host probe.
JOB_PROBE = "probe"
# Run an Ansible playbook. The worker IS the control node, which is what
# retires the "nominate one of the customer's Linux boxes" workaround.
JOB_ANSIBLE = "ansible"
# terraform init/plan/apply/destroy in a working directory on the worker.
JOB_TERRAFORM = "terraform"
# Push switch CLI over SSH with Netmiko. VXLAN, BGP and Config Builder deploys.
JOB_NETMIKO = "netmiko"
# Sweep a CIDR for reachable hosts, so an engineer can add an inventory without
# typing twenty addresses that only exist inside the customer's network.
JOB_DISCOVER = "discover"

JOB_KINDS = (
    JOB_SHELL, JOB_SSH, JOB_PROBE, JOB_ANSIBLE,
    JOB_TERRAFORM, JOB_NETMIKO, JOB_DISCOVER,
)


def frame(frame_type: str, /, **fields: Any) -> str:
    """One frame, ready to send.

    `frame_type` is positional-only (that is what the `/` does). Without it,
    `frame(JOB, kind="ansible", ...)` collides with the parameter name and
    raises "got multiple values for argument" - which is exactly what happened
    the first time a job was dispatched, and the traceback points at the call
    site rather than at this signature.
    """
    return json.dumps({"t": frame_type, **fields}, separators=(",", ":"))


def parse(raw: str | bytes) -> dict:
    """A received frame, or `{}` for anything unreadable.

    Never raises. A malformed frame from either side must not take down a
    connection that is otherwise carrying a running job - it is dropped, and the
    caller decides whether that matters.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("t"), str) else {}


def split_token(token: str) -> tuple[str, str]:
    """`psw_<public_id>_<secret>` -> (public_id, secret).

    The token carries its own lookup key so the supervisor can fetch one row and
    verify one hash, rather than bcrypt-checking every worker it has ever issued
    on every connection attempt. Returns ("", "") for anything malformed - the
    caller then fails authentication without a separate shape check.
    """
    text = (token or "").strip()
    if not text.startswith("psw_"):
        return "", ""
    parts = text.split("_", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return "", ""
    return parts[1], parts[2]


def build_token(public_id: str, secret: str) -> str:
    return f"psw_{public_id}_{secret}"
