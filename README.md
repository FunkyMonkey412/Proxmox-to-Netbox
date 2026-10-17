# Proxmox to NetBox Sync

Synchronises all QEMU VMs and LXC containers from a Proxmox cluster into NetBox, including interfaces, MAC addresses, and IP addresses.

## What it syncs

| Data | Source | NetBox destination |
|------|--------|--------------------|
| VMs & containers | Proxmox QEMU + LXC | `virtualization.virtual_machines` |
| Network interfaces | Proxmox VM config (`net0`, `net1`, …) | `virtualization.interfaces` |
| MAC addresses | Parsed from Proxmox net config | `dcim.mac_addresses` (NetBox 4.x) |
| IP addresses | QEMU guest agent / LXC interfaces API | `ipam.ip_addresses` |


## Setup

See [SETUP.md](SETUP.md) for detailed instructions on configuring Proxmox and NetBox, installing the guest agent, and setting up permissions.

## Requirements

```bash
apt install python3-venv
pip install proxmoxer pynetbox requests python-dotenv
```

- NetBox 4.x (MAC addresses are first-class objects, not interface fields)
- QEMU VMs need the **guest agent installed and running** for IP discovery

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```ini
# Required
PROXMOX_HOST=              # IP or hostname of your Proxmox node
PROXMOX_TOKEN_USER=netboxsync@pam  # user@realm of the API token owner
PROXMOX_TOKEN_ID=netbox-sync       # token ID
PROXMOX_TOKEN_SECRET=              # token secret (UUID)

NETBOX_URL=                # e.g. https://192.168.1.10
NETBOX_TOKEN=              # NetBox API token

# Optional
CLUSTER_NAME=              # NetBox cluster name (default: Proxmox-Cluster)
CLUSTER_TYPE_NAME=         # NetBox cluster type name (default: Proxmox)
```

> The script disables SSL verification — intended for self-signed certificates on a home lab.

## Usage

From the `Proxmox-to-Netbox` folder:

```bash
venv/bin/python3 proxmox-netbox-sync.py
```

## How it works

1. **Cluster** — ensures a `Proxmox-Cluster` cluster exists in NetBox
2. **VMs** — iterates every node, processes QEMU VMs and LXC containers; creates or updates the NetBox VM record with status, vCPUs, and memory
3. **Interfaces** — syncs `net0`, `net1`, … entries from the Proxmox VM config
4. **MAC addresses** — creates/updates `dcim.mac_addresses` objects linked to the interface and sets them as the primary MAC (NetBox 4.x model)
5. **IP addresses** — queries the QEMU guest agent or LXC interfaces API; skips loopback and link-local addresses


## MAC address parsing

Proxmox uses different formats depending on VM type:

- **QEMU:** `virtio=BC:24:11:B0:A7:6A,bridge=vmbr0,firewall=1` — driver name is the key
- **LXC:** `name=eth0,bridge=vmbr0,hwaddr=BC:24:11:F9:F5:04,ip=dhcp` — `hwaddr` is the key
