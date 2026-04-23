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

### API token

The script authenticates to the Proxmox API using an API token. To set one up:

1. Go to **Datacenter → Permissions → Roles** → **Add**
   - Name: `NetBoxSync`
   - Privileges: `VM.Audit`, `Sys.Audit`, `VM.GuestAgent.Unrestricted`
2. Go to **Datacenter → Permissions → Users** → **Add**
   - Username: `netboxsync`, Realm: `pam`
3. Go to **Datacenter → Permissions → Add → User Permission**
   - Path: `/`, User: `netboxsync@pam`, Role: `NetBoxSync`
4. Go to **Datacenter → Permissions → API Tokens** → **Add**
   - User: `netboxsync@pam`, Token ID: `netbox-sync`
   - Uncheck **Privilege Separation** (so the token inherits the user's permissions)
   - Copy the generated secret into your `.env` as `PROXMOX_TOKEN_SECRET`

The token ID and user are pre-filled in `.env.example`. You only need to add the secret.

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

```bash
git clone git@github.com:FunkyMonkey412/Proxmox-to-Netbox.git
cd Proxmox-to-Netbox

apt install python3-venv
python3 -m venv venv
venv/bin/pip install proxmoxer pynetbox requests python-dotenv

cp .env.example .env
# Edit .env with your values
```

## 4. Running the script

From the `Proxmox-to-Netbox` folder:

```bash
venv/bin/python3 proxmox-netbox-sync.py
```

---

## 5. Feature matrix

| Feature | What's needed |
|---------|--------------|
| VM/container records | Proxmox API access |
| Network interfaces + MACs | Proxmox API access |
| LXC IP addresses | Proxmox API access (built-in, no agent) |
| QEMU VM IP addresses | QEMU guest agent installed and enabled |
| QEMU VM open TCP services | QEMU guest agent with exec support |
