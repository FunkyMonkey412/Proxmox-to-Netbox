# Setup Guide

This guide covers everything you need to configure in Proxmox and NetBox before running the sync script.

---

## 1. NetBox

### API token

The script needs a NetBox API token with read/write access to:
- `virtualization` — clusters, VMs, interfaces
- `dcim` — MAC addresses (NetBox 4.x)
- `ipam` — IP addresses, services

To create one:

1. Log in to NetBox
2. Go to your profile (top-right) → **API Tokens**
3. Click **Add a token**
4. Leave all permissions at their defaults (full access) unless you want to lock it down further
5. Copy the token into your `.env` as `NETBOX_TOKEN`

### NetBox version

This script requires **NetBox 4.x**. MAC addresses in 4.x are standalone objects under `dcim/mac-addresses`, not plain fields on interfaces. Earlier versions use a different data model and are not compatible.

---

## 2. Proxmox

### Create the sync user

The script uses a single dedicated user (`netboxsync@pam`) for both Proxmox API access and SSH-based LXC service discovery.

**Step 1 — Create the Linux user on the Proxmox host:**

> **Important:** The `@pam` realm means Proxmox delegates authentication to the Linux operating system. The Proxmox web UI user alone is not enough — a matching Linux system user **must** also exist on the host, or login will fail with a 401 error.

Run this on the Proxmox host (e.g. via SSH or the shell tab in the UI):

```bash
useradd -M -s /usr/sbin/nologin netboxsync
passwd netboxsync
```

- `-M` — no home directory (this is a service account)
- `-s /usr/sbin/nologin` — disables interactive shell login for security

Set the same password you will use in your `.env` file as `PROXMOX_PASSWORD`.

**Step 2 — Grant sudo access for LXC container inspection:**

```bash
echo 'netboxsync ALL=(root) NOPASSWD: /usr/sbin/pct exec *' > /etc/sudoers.d/netboxsync
chmod 440 /etc/sudoers.d/netboxsync
```

> Without this step, LXC port discovery is silently skipped. All other sync features still work.

**Step 3 — Add the user in the Proxmox web UI:**

Go to **Datacenter → Permissions → Users → Add**:
- Username: `netboxsync`
- Realm: `Linux PAM standard authentication`

**Step 4 — Create a role:**

Go to **Datacenter → Permissions → Roles → Add**:
- Name: `NetBoxSync`
- Privileges: `Sys.Audit`, `VM.Audit`, `VM.GuestAgent.Unrestricted`

> `VM.GuestAgent.Unrestricted` was introduced in Proxmox VE 9. On older versions, use `VM.Monitor` instead.

**Step 5 — Assign the role:**

Go to **Datacenter → Permissions → Add → User Permission**:
- Path: `/`
- User: `netboxsync@pam`
- Role: `NetBoxSync`

Set `PROXMOX_USER=netboxsync@pam` and `PROXMOX_PASSWORD=<password>` in your `.env`.

### QEMU guest agent (required for VM IP and service discovery)

For QEMU VMs, IPs and open ports are discovered via the QEMU guest agent. Without it, the VM record is still created in NetBox but IP addresses and services will be missing.

**Install the guest agent inside each VM:**

Debian/Ubuntu:
```bash
apt install qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

RHEL/CentOS/Rocky:
```bash
dnf install qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

**Enable the guest agent in Proxmox:**

1. Select the VM → **Hardware** → **Add** → **QEMU Guest Agent**, or
2. Select the VM → **Options** → **QEMU Guest Agent** → **Enabled: yes**

A reboot of the VM is required after enabling it in Proxmox if the VM was already running.

> If the guest agent is not running (e.g. VM is off, or agent not installed), IP and service discovery is silently skipped for that VM.

---

## 3. Installation

On Debian/Ubuntu, install Python 3 and venv support first:

```bash
apt install python3 python3-venv
```

Then clone the repo and set up the environment:

```bash
git clone https://github.com/FunkyMonkey412/Proxmox-to-Netbox.git
cd Proxmox-to-Netbox

python3 -m venv venv
venv/bin/pip install proxmoxer pynetbox requests python-dotenv paramiko

cp .env.example .env
# Edit .env with your values
```

Once the script runs successfully, consider scheduling it with a cronjob to keep NetBox in sync automatically.

---

## 4. Feature matrix

| Feature | What's needed |
|---------|--------------|
| VM/container records | `VM.Audit` + `Sys.Audit` |
| Network interfaces + MACs | `VM.Audit` |
| LXC IP addresses | `VM.Audit` (built-in, no agent needed) |
| QEMU VM IP addresses | `VM.GuestAgent.Unrestricted` + guest agent installed and running |
| LXC open TCP services | SSH to Proxmox host + sudo for `pct exec` + `ss` in container |
| QEMU VM open TCP services | `VM.GuestAgent.Unrestricted` + guest agent with exec support |
