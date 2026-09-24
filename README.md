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

There is no telemetry, no analytics, no automatic update check, and no address
baked into this code. The Supervisor address is supplied by whoever runs the
installer. Updates are something you run deliberately — see
[Updating](#updating-an-existing-worker).

---

## Requirements

* Ubuntu 22.04, 24.04 or newer (Debian works; Rocky/RHEL is untested)
* **2 vCPU, 4 GB RAM, 40 GB disk** as a sound minimum

  Go to 4 vCPU / 8 GB only for large Kubernetes builds. **Disk matters more
  than RAM**: Terraform providers, container images and Ansible collections all
  land on the root filesystem, and a worker that runs out of room there fails
  every job with an error that never mentions disk.
* Outbound TCP to your Supervisor's address on its agent port (9998 by default)
* Outbound HTTPS to GitHub, PyPI and the Ubuntu archives **for the install
  itself**. The agent needs none of that once it is running.
* `sudo` for the install; the agent afterwards runs unprivileged

---

## Installing

Create the worker in the Proseth platform first — that is what issues the
token, and it is shown only once.

Either clone the repository:

```bash
git clone https://github.com/prosethsolutionsAI/proseth-worker.git
cd proseth-worker
sudo ./install.sh
```

…or take just the installer, which fetches the rest itself:

```bash
curl -fsSLO https://raw.githubusercontent.com/prosethsolutionsAI/proseth-worker/main/install.sh
sudo bash install.sh
```

Both work. If the agent is not sitting beside `install.sh`, the installer
downloads it and says so.

The installer asks four things:

| It asks for | Why |
|---|---|
| The Supervisor's address | It may be a private address across a tunnel or a public one. Only you know which this machine can reach. |
| The port | Defaults to 9998. |
| Whether to use TLS | `wss://` instead of `ws://`. **Answer yes only if TLS has been terminated in front of your Supervisor.** The installer checks the port and corrects the setting if the answer does not match what is there. Asked every time rather than remembered, so a wrong answer cannot persist. |
| The worker name and token | Issued when the worker was created in the platform. The name should match what you called it there. |

It then checks it can actually reach the Supervisor **before** changing
anything, installs Python, Ansible, Terraform, kubectl and helm, creates an
unprivileged `proseth` service account, writes a systemd unit, and starts it.

### Unattended

```bash
sudo PROSETH_NONINTERACTIVE=1 \
     PROSETH_SUPERVISOR_HOST=203.0.113.10 \
     PROSETH_SUPERVISOR_PORT=9998 \
     PROSETH_TOKEN=psw_xxxxxxxx_yyyyyyyy \
     PROSETH_WORKER_NAME=acme-dc-01 \
     bash install.sh
```

Anything you leave out is still asked for, so a half-filled environment falls
back to the wizard rather than failing. On a machine that already has a
configuration, anything you leave out keeps its existing value.

Re-running the installer is safe. It skips what is already present, keeps the
existing configuration, and is the supported way to pick up new tooling.

---

## Versions and updating

### Which version is this worker running?

```bash
proseth-worker --check
```

prints the agent version along with what this machine looks like. The same
version is sent to the Supervisor when the worker connects and is shown beside
the worker on its **Workers** page — so you can see what every site is running
without logging into any of them.

### Updating an existing worker

From the Supervisor, press **Upgrade** on the worker's card — it appears
whenever that worker is behind. Or on the box:

```bash
sudo proseth-worker-update
```

That is the whole thing. It downloads the current agent, re-installs it, and
restarts the service, **keeping this worker's address, port, name, token and
TLS setting** — you are not asked anything and nothing needs to be re-entered.

It takes a few seconds, and the worker reconnects on its own. Any job running
at that moment is lost, so update when the worker is idle.

If the machine has no route to GitHub, copy the repository onto it and run
`sudo ./install.sh` from inside it instead — that path needs no internet
beyond PyPI and the Ubuntu archives.

### Changing the address or the token

```bash
sudo proseth-worker-setup
```

Re-runs the wizard with the current values filled in; press Enter to keep each
one. This uses the copy of the installer in `/opt/proseth-worker`, so it works
with no internet at all.

### Version history

| Version | What changed |
|---|---|
| 1.1.5 | `proseth-worker --check` works from any directory. The launcher did not set `PYTHONPATH` — only the systemd unit did — so the service ran fine while the command the installer prints and this file documents failed with `No module named 'proseth_worker'` for anyone not standing in `/opt`. |
| 1.1.4 | **One-click upgrade from the Supervisor.** A worker showing an available update can be upgraded from its card rather than by finding a shell on it. The agent is granted exactly one command through sudo — a root-owned wrapper taking no arguments — which starts the ordinary installer in a separate systemd unit so restarting the service cannot kill the update half way through. The worker still fetches the release itself; nothing is pushed to it. |
| 1.1.3 | The agent can be given the Supervisor's certificate (`ca_cert`), so TLS can be verified rather than merely encrypted. The installer fetches it, stores it beside the config and prints its fingerprint, and it now turns TLS **on** by itself if the port is using it — the correction already worked in the other direction. |
| 1.1.2 | Quieter install: the TLS prompt no longer comments on the Supervisor, and the reachability check reports only whether it reached it. |
| 1.1.1 | **TLS now works at all.** A `wss://` connection was built with no SSL context whenever certificate verification was left on — which is the default — so the agent retried for ever with `ssl=None is incompatible with a wss:// URI`. Only the insecure variant had ever worked. TLS also now defaults to **off** at the prompt and is asked rather than inherited, and the installer checks what actually answered on the port: it confirms it reached the Supervisor, and corrects the TLS setting if it does not match what is available there. |
| 1.1.0 | Installing from `install.sh` alone now works — it fetches the agent rather than failing. A failed install stops and says so instead of reporting success. `proseth-worker-setup` no longer destroys the agent it is re-configuring. An unattended re-run keeps the TLS setting. Added `proseth-worker-update`. |
| 1.0.0 | First release. |

---

## Operating it

```bash
systemctl status proseth-worker        # is it running
journalctl -u proseth-worker -f        # what is it doing
sudo systemctl restart proseth-worker  # after changing the configuration
```

| Path | What is there |
|---|---|
| `/opt/proseth-worker` | The agent's own code, its Python venv, and a copy of the installer. Root-owned; the service cannot write to it. |
| `/etc/proseth-worker/config.json` | Supervisor address, port, TLS, worker name, token. Mode `640`, readable by root and the `proseth` service account only. |
| `/etc/proseth-worker/supervisor.crt` | The Supervisor's certificate, when TLS is in use — this is what the agent verifies against. Public; mode `644`. |
| `/var/lib/proseth-worker` | Everything the agent writes: Ansible temp, Terraform working directories, SSH known hosts. This is also the service account's `$HOME` — Ansible refuses to start without a writable one. |

| Command | What it does |
|---|---|
| `proseth-worker --check` | Version, hostname, distro, CPU, memory, disk. Touches nothing. |
| `sudo proseth-worker-update` | Fetch and install the current agent, keeping the configuration. |
| `sudo proseth-worker-setup` | Re-run the wizard to change the address or token. |

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
proseth_worker/agent.py       the connection, reconnect and job loop; VERSION lives here
proseth_worker/jobs.py        the job handlers
proseth_worker/facts.py       CPU, memory, disk — read from /proc, no psutil
proseth_worker/protocol.py    the wire format
```

`protocol.py` is byte-identical to the Supervisor's copy and imports nothing
but the standard library. If you change it, change both.

---

## Security notes

* The service account cannot write to `/opt/proseth-worker`, so the agent
  cannot rewrite its own code.
* It is granted **exactly one command** through sudo, in
  `/etc/sudoers.d/proseth-worker`: `proseth-worker-selfupdate`, root-owned, in
  a directory the account cannot write to, and taking **no arguments** — so
  there is nothing to smuggle through it. It is what makes Upgrade work from
  the Supervisor, and it grants nothing else. Remove that file if you would
  rather upgrade only from the box; everything else keeps working.
* The token is stored in `/etc/proseth-worker/config.json`, mode `640`,
  root-owned and readable by the service account. The Supervisor keeps only a
  bcrypt hash of it and cannot read it back.
* SSH keys and inventories written during a job go into a `mktemp -d` at 0700
  with the files at 0600, and are removed by a shell `trap` on every exit path.
* Secrets are never passed as command-line arguments — they would be visible in
  the process list to anything else on the machine.
* **The connection is a plain WebSocket unless TLS is enabled at both ends.**
  Where the Supervisor offers TLS, the installer detects it, turns it on, and
  fetches the certificate the agent then verifies against — so `wss://` here is
  encrypted *and* authenticated, not merely encrypted. Where it does not, the
  traffic is in clear: **run it across a network you trust, or a tunnel.**
  Saying so is better than implying the default is encrypted.
* The certificate is fetched from the Supervisor at install time, which is
  trust-on-first-use — an interception at that moment could substitute its
  own. The installer prints the fingerprint; compare it against the
  Supervisor's if the path between them is not trusted, or copy the file
  across by hand instead.

---

## Removing it

```bash
sudo systemctl disable --now proseth-worker
sudo rm -rf /opt/proseth-worker /etc/proseth-worker /var/lib/proseth-worker
sudo rm -f /etc/systemd/system/proseth-worker.service
sudo rm -f /usr/local/bin/proseth-worker /usr/local/bin/proseth-worker-setup \
           /usr/local/bin/proseth-worker-update
sudo systemctl daemon-reload
sudo userdel proseth
```

Removing the worker record in the platform does **not** uninstall anything
here; the agent keeps running and is refused the next time it reconnects.

---

## Licence

MIT — see [LICENSE](LICENSE).
