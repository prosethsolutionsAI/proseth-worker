# Proseth Worker

The deploy agent for the Proseth Engineer System.

A **Worker** is a small Ubuntu machine at your site. It connects **outbound** to
a Proseth Supervisor and carries out work from inside your network — running
Ansible playbooks, SSH scripts, Terraform, and switch configuration.

This repository contains the agent and its installer, and nothing else. It is
public so that you can read exactly what you are about to install before you
install it.

---

## What it does, and what it does not

**It connects out. Nothing connects in.**

The worker opens a WebSocket to the Supervisor's address on port 9998 and keeps
it open. Jobs arrive down that connection; output streams back up it. There is
no listening port, no inbound firewall rule to open, no VPN, and no need for a
static address at your end.

**It only does what it is told, by one Supervisor.**

The agent is given a token at install time. That token is what ties this
machine to one Supervisor, and it is the only thing that does. Point it at a
different address and it will not connect; rotate the token and it stops.

**It does not phone home to us.**

There is no telemetry, no analytics, no update check, and no address baked into
this code. The Supervisor address is supplied by whoever runs the installer.

---

## Requirements

* Ubuntu 22.04, 24.04 or newer (Debian works; Rocky/RHEL is untested)
* **2 vCPU, 4 GB RAM, 40 GB disk** as a sound minimum

  Go to 4 vCPU / 8 GB only for large Kubernetes builds. **Disk matters more
  than RAM**: Terraform providers, container images and Ansible collections all
  land on the root filesystem, and a worker that runs out of room there fails
  every job with an error that never mentions disk.
* Outbound TCP to your Supervisor's address on its agent port (9998 by default)
* `sudo` for the install itself; the agent afterwards runs unprivileged

---

## Installing

Create the worker in the Proseth platform first — that is what issues the
token, and it is shown only once.

```bash
curl -fsSLO https://raw.githubusercontent.com/OWNER/proseth-worker/main/install.sh
sudo bash install.sh
```

The installer asks three things:

| It asks for | Why |
|---|---|
| The Supervisor's address | It may be a private address across a tunnel or a public one. Only you know which this machine can reach. |
| The port | Defaults to 9998. |
| The token | Issued when the worker was created in the platform. |

It then installs Python, Ansible, Terraform, kubectl and helm, creates an
unprivileged `proseth` service account, writes a systemd unit, and starts it.

### Unattended

```bash
sudo PROSETH_NONINTERACTIVE=1 \
     PROSETH_SUPERVISOR_HOST=203.0.113.10 \
     PROSETH_PORT=9998 \
     PROSETH_TOKEN=psw_xxxxxxxx_yyyyyyyy \
     PROSETH_WORKER_NAME=acme-dc-01 \
     bash install.sh
```

Re-running the installer is safe. It skips what is already present and is the
supported way to pick up new tooling.

---

## Operating it

```bash
systemctl status proseth-worker        # is it running
journalctl -u proseth-worker -f        # what is it doing
sudo systemctl restart proseth-worker  # after changing the configuration
```

| Path | What is there |
|---|---|
| `/opt/proseth-worker` | The agent's own code. Root-owned; the service cannot write to it. |
| `/etc/proseth-worker/worker.env` | Supervisor address, port, token. Readable only by root. |
| `/var/lib/proseth-worker` | Everything the agent writes: Ansible temp, Terraform working directories, SSH known hosts. |

### Checking it without starting the service

```bash
sudo -u proseth /opt/proseth-worker/venv/bin/python -m proseth_worker.agent --check
```

Connects, authenticates, reports what it found, and exits.

---

## What it can be asked to do

| Job | What it means |
|---|---|
| `shell` | A bash script on the worker itself |
| `ssh` | A script piped to `bash` on another machine, over SSH |
| `probe` | Connect, confirm the login works, report distro/CPU/memory/disk |
| `ansible` | A playbook, with this worker as the control node |
| `terraform` | Terraform in a working directory on this worker |
| `netmiko` | Switch CLI over SSH, or a `show` command |
| `discover` | A TCP connect sweep of one port across a range |

Everything is initiated by the Supervisor. The agent never decides to do
anything on its own, and every job is recorded in the Supervisor's audit log
against the person who asked for it.

Credentials for the machines a job touches arrive **with that job** and live in
memory for its duration. They are not stored on the worker. A worker should
therefore be trusted the way a jump host is.

---

## Layout

```
install.sh                    the installer and its wizard
proseth_worker/agent.py       the connection, reconnect and job loop
proseth_worker/jobs.py        the job handlers
proseth_worker/facts.py       CPU, memory, disk — read from /proc, no psutil
proseth_worker/protocol.py    the wire format
```

`protocol.py` is byte-identical to the Supervisor's copy and imports nothing
but the standard library. If you change it, change both.

---

## Security notes

* The service account has **no sudo** and cannot write to `/opt/proseth-worker`.
* The token is stored in `/etc/proseth-worker/worker.env`, mode 0600, root-only.
  The Supervisor keeps only a bcrypt hash of it and cannot read it back.
* SSH keys and inventories written during a job go into a `mktemp -d` at 0700
  with the files at 0600, and are removed by a shell `trap` on every exit path.
* Secrets are never passed as command-line arguments — they would be visible in
  the process list to anything else on the machine.
* The connection is a plain WebSocket today. **Run it across a network you
  trust, or a tunnel.** TLS is the next thing on this list, and saying so is
  better than implying it is already there.

---

## Removing it

```bash
sudo systemctl disable --now proseth-worker
sudo rm -rf /opt/proseth-worker /etc/proseth-worker /var/lib/proseth-worker
sudo rm -f /etc/systemd/system/proseth-worker.service
sudo systemctl daemon-reload
sudo userdel proseth
```

Removing the worker record in the platform does **not** uninstall anything
here; the agent keeps running and is refused the next time it reconnects.

---

## Licence

MIT — see [LICENSE](LICENSE).
