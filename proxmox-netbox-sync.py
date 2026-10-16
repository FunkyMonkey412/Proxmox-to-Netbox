#!/usr/bin/env python3
"""
Proxmox to NetBox Sync Script
Save as: /opt/scripts/proxmox-netbox-sync.py

Copy .env.example to .env and fill in your credentials before running.
"""

from proxmoxer import ProxmoxAPI
import pynetbox
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
import urllib3
import re
import time
import os
from dotenv import load_dotenv

load_dotenv()

MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')

# Disable SSL warnings if using self-signed certs
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def nb_get(endpoint, params=None, only=None):
    """Raw GET request to NetBox API with optional field limiting."""
    url = f"{NETBOX_URL}/api/{endpoint}/"
    headers = {"Authorization": f"Token {NETBOX_TOKEN}", "Content-Type": "application/json"}
    params = params or {}
    if only:
        params['only'] = ','.join(only)
    response = session.get(url, headers=headers, params=params, verify=False)
    response.raise_for_status()
    return response.json()


def nb_post(endpoint, data):
    """Raw POST request to NetBox API."""
    url = f"{NETBOX_URL}/api/{endpoint}/"
    headers = {"Authorization": f"Token {NETBOX_TOKEN}", "Content-Type": "application/json"}
    response = session.post(url, headers=headers, json=data, verify=False)
    response.raise_for_status()
    return response.json()


def nb_patch(endpoint, data, obj_id):
    """Raw PATCH request to NetBox API."""
    url = f"{NETBOX_URL}/api/{endpoint}/{obj_id}/"
    headers = {"Authorization": f"Token {NETBOX_TOKEN}", "Content-Type": "application/json"}
    response = session.patch(url, headers=headers, json=data, verify=False)
    response.raise_for_status()
    return response.json()



# Configuration
PROXMOX_HOST = os.environ["PROXMOX_HOST"]
PROXMOX_TOKEN_USER = os.environ["PROXMOX_TOKEN_USER"]
PROXMOX_TOKEN_ID = os.environ["PROXMOX_TOKEN_ID"]
PROXMOX_TOKEN_SECRET = os.environ["PROXMOX_TOKEN_SECRET"]

NETBOX_URL = os.environ["NETBOX_URL"]
NETBOX_TOKEN = os.environ["NETBOX_TOKEN"]

CLUSTER_NAME = os.getenv("CLUSTER_NAME", "Proxmox-Cluster")
CLUSTER_TYPE_NAME = os.getenv("CLUSTER_TYPE_NAME", "Proxmox")

# Connect to Proxmox
proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_TOKEN_USER,
    token_name=PROXMOX_TOKEN_ID,
    token_value=PROXMOX_TOKEN_SECRET,
    verify_ssl=False
)

# Connect to NetBox with SSL verification disabled
session = requests.Session()
session.verify = False
nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
nb.http_session = session

QEMU_NIC_TYPES = {
    'virtio', 'e1000', 'e1000-82540em', 'e1000-82544gc',
    'e1000-82545em', 'vmxnet3', 'rtl8139', 'ne2k_pci', 'pcnet'
}

def parse_mac_from_config(config_value, vm_type='qemu'):
    """Extract MAC address from a Proxmox network config string.

    QEMU format: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,...
    LXC format:  name=eth0,hwaddr=AA:BB:CC:DD:EE:FF,bridge=vmbr0,...
    """
    for part in config_value.split(','):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k, v = k.strip(), v.strip()
        if vm_type == 'qemu' and k in QEMU_NIC_TYPES and MAC_PATTERN.match(v):
            return v
        if k in ('hwaddr', 'macaddr') and MAC_PATTERN.match(v):
            return v
    return None


def get_or_create_cluster():
    """Get or create cluster in NetBox"""
    # Try to get existing cluster
    try:
        cluster = nb.virtualization.clusters.get(name=CLUSTER_NAME, only=['id', 'name', 'type'])
        if cluster:
            print(f"Found existing cluster: {CLUSTER_NAME}")
            return cluster
    except:
        pass

    # Create cluster type if needed
    cluster_type_slug = CLUSTER_TYPE_NAME.lower().replace(" ", "-")
    try:
        cluster_type = nb.virtualization.cluster_types.get(name=CLUSTER_TYPE_NAME, only=['id', 'name', 'slug'])
        if not cluster_type:
            print(f"Creating cluster type: {CLUSTER_TYPE_NAME}")
            cluster_type = nb.virtualization.cluster_types.create(
                name=CLUSTER_TYPE_NAME,
                slug=cluster_type_slug
            )
        else:
            print(f"Found existing cluster type: {CLUSTER_TYPE_NAME}")
    except:
        print(f"Creating cluster type: {CLUSTER_TYPE_NAME}")
        cluster_type = nb.virtualization.cluster_types.create(
            name=CLUSTER_TYPE_NAME,
            slug=cluster_type_slug
        )
    
    # Create cluster
    print(f"Creating cluster: {CLUSTER_NAME}")
    cluster = nb.virtualization.clusters.create(
        name=CLUSTER_NAME,
        type=cluster_type.id
    )
    return cluster

def get_vm_ip_addresses(node_name, vm_id, vm_type='qemu'):
    """Get IP addresses from VM using QEMU guest agent or LXC"""
    ip_addresses = []
    
    try:
        if vm_type == 'qemu':
            # Try to get network interfaces from QEMU guest agent
            network_data = proxmox.nodes(node_name).qemu(vm_id).agent.get('network-get-interfaces')
            
            if network_data and 'result' in network_data:
                for interface in network_data['result']:
                    if 'ip-addresses' in interface:
                        for ip_info in interface['ip-addresses']:
                            ip_address = ip_info.get('ip-address')
                            ip_type = ip_info.get('ip-address-type')
                            
                            # Skip unwanted addresses
                            if not ip_address:
                                continue
                            
                            # Skip loopback addresses (127.0.0.0/8 and ::1)
                            if ip_address.startswith('127.') or ip_address == '::1':
                                continue
                            
                            # Skip link-local addresses (169.254.0.0/16 and fe80::/10)
                            if ip_address.startswith('169.254.') or ip_address.startswith('fe80:'):
                                continue
                            
                            # Skip loopback interfaces
                            if interface.get('name', '').startswith('lo'):
                                continue
                            
                            prefix = ip_info.get('prefix', 24 if ip_type == 'ipv4' else 64)
                            ip_addresses.append({
                                'address': f"{ip_address}/{prefix}",
                                'interface': interface.get('name', 'unknown')
                            })
        
        elif vm_type == 'lxc':
            # For LXC containers, get IP from container config
            network_data = proxmox.nodes(node_name).lxc(vm_id).interfaces.get()
            
            for interface in network_data:
                # Skip loopback interface
                if interface.get('name', '').startswith('lo'):
                    continue
                    
                if 'inet' in interface:
                    ip = interface['inet'].split('/')[0]
                    # Skip loopback and link-local
                    if not ip.startswith('127.') and not ip.startswith('169.254.'):
                        ip_addresses.append({
                            'address': interface['inet'],
                            'interface': interface.get('name', 'eth0')
                        })
                
                if 'inet6' in interface:
                    ip = interface['inet6'].split('/')[0]
                    # Skip loopback and link-local
                    if ip != '::1' and not ip.startswith('fe80:'):
                        ip_addresses.append({
                            'address': interface['inet6'],
                            'interface': interface.get('name', 'eth0')
                        })
    
    except Exception as e:
        # Guest agent might not be installed or VM might be off
        pass
    
    return ip_addresses

def sync_vms():
    """Sync all VMs from Proxmox to NetBox"""
    cluster = get_or_create_cluster()
    
    # Get all nodes
    for node in proxmox.nodes.get():
        node_name = node['node']
        print(f"\nProcessing node: {node_name}")
        
        # Process QEMU VMs
        try:
            vms = proxmox.nodes(node_name).qemu.get()
            for vm in vms:
                process_vm(node_name, vm, cluster, 'qemu')
        except Exception as e:
            print(f"Could not get VMs from {node_name}: {e}")
        
        # Process LXC Containers
        try:
            containers = proxmox.nodes(node_name).lxc.get()
            for container in containers:
                process_vm(node_name, container, cluster, 'lxc')
        except Exception as e:
            print(f"Could not get containers from {node_name}: {e}")

def process_vm(node_name, vm, cluster, vm_type='qemu'):
    """Process a single VM or container"""
    vm_name = vm['name']
    vm_id = vm['vmid']
    status = 'active' if vm['status'] == 'running' else 'offline'
    
    type_label = "Container" if vm_type == 'lxc' else "VM"
    print(f"  Processing {type_label}: {vm_name} (ID: {vm_id})")
    
    # Get detailed VM config
    try:
        if vm_type == 'qemu':
            vm_config = proxmox.nodes(node_name).qemu(vm_id).config.get()
        else:
            vm_config = proxmox.nodes(node_name).lxc(vm_id).config.get()
    except Exception as e:
        print(f"    Could not get config for {vm_name}: {e}")
        return
    
    # Extract VM details
    if vm_type == 'qemu':
        vcpus = vm_config.get('cores', 1) * vm_config.get('sockets', 1)
        memory = vm_config.get('memory', 0)
    else:
        vcpus = vm_config.get('cores', 1)
        memory = vm_config.get('memory', 0)
    
    # Check if VM already exists in NetBox using raw API
    vm_data = {
        'name': vm_name,
        'cluster': cluster.id,
        'status': status,
        'vcpus': vcpus,
        'memory': memory,
        'comments': f'Proxmox {type_label} ID: {vm_id}\nNode: {node_name}\nType: {vm_type}'
    }
    
    try:
        # Search for existing VM using raw API to avoid _config_context_data
        results = nb_get('virtualization/virtual-machines', params={'name': vm_name}, only=['id', 'name', 'status', 'vcpus', 'memory', 'comments', 'cluster'])
        existing_vm = results.get('results', [{}])[0] if results.get('count', 0) > 0 else None
        
        if existing_vm:
            # Update existing VM using raw PATCH
            nb_patch(f'virtualization/virtual-machines/{existing_vm["id"]}', vm_data)
            existing_vm = dict(existing_vm, **vm_data)  # Merge updates
            print(f"    ✓ Updated {type_label}: {vm_name}")
        else:
            # Create new VM using raw POST
            existing_vm = nb_post('virtualization/virtual-machines', vm_data)
            print(f"    ✓ Created {type_label}: {vm_name}")
    except Exception as e:
        print(f"    ✗ Error with {type_label} {vm_name}: {e}")
        return
    
    # Sync network interfaces and IPs
    sync_vm_interfaces(node_name, vm_id, vm_name, vm_type, status)

    # Sync open TCP ports as services (only for running VMs)
    if status == 'active':
        print(f"    Discovering open TCP ports on {vm_name}...")
        ports = get_open_tcp_ports(node_name, vm_id, vm_type)
        if ports:
            print(f"      Found services: { {k: v for k, v in ports.items()} }")
        else:
            print(f"      No open ports found (agent unavailable or none listening)")
        sync_services(existing_vm, ports)

def sync_vm_interfaces(node_name, vm_id, vm_name, vm_type='qemu', vm_status='offline'):
    """Sync VM network interfaces and IP addresses"""
    try:
        if vm_type == 'qemu':
            vm_config = proxmox.nodes(node_name).qemu(vm_id).config.get()
        else:
            vm_config = proxmox.nodes(node_name).lxc(vm_id).config.get()
        
        vm = nb.virtualization.virtual_machines.get(name=vm_name, only=['id', 'name', 'status'])
        
        if not vm:
            return
        
        # Get IP addresses from guest agent (only if VM is running)
        ip_data = []
        if vm_status == 'active':
            ip_data = get_vm_ip_addresses(node_name, vm_id, vm_type)
            if ip_data:
                print(f"      Found {len(ip_data)} IP address(es)")
        
        # Look for network interfaces (net0, net1, etc.)
        for key, value in vm_config.items():
            if key.startswith('net'):
                mac = parse_mac_from_config(value, vm_type)
                
                interface_name = key
                
                # Check if interface exists using raw API
                int_data = {
                    'virtual_machine': existing_vm['id'],
                    'name': interface_name,
                    'type': 'virtual'
                }
                
                try:
                    # Search for existing interface
                    results = nb_get('virtualization/interfaces', params={
                        'virtual_machine_id': existing_vm['id'],
                        'name': interface_name
                    }, only=['id', 'name', 'virtual_machine', 'type'])
                    existing_int = results.get('results', [{}])[0] if results.get('count', 0) > 0 else None
                    
                    if existing_int:
                        # Update using raw PATCH
                        nb_patch(f'virtualization/interfaces/{existing_int["id"]}', int_data)
                        interface = dict(existing_int, **int_data)
                        print(f"      ✓ Updated interface: {interface_name}")
                    else:
                        # Create using raw POST
                        interface = nb_post('virtualization/interfaces', int_data)
                        print(f"      ✓ Created interface: {interface_name}")

                    if mac:
                        sync_mac_address(mac, interface)

                    # Assign IP addresses to this interface
                    if ip_data:
                        for ip_info in ip_data:
                            assign_ip_to_interface(ip_info['address'], interface, vm)

                except Exception as e:
                    print(f"      ✗ Error with interface {interface_name}: {e}")
                
    except Exception as e:
        print(f"    Error syncing interfaces: {e}")

def sync_mac_address(mac, interface):
    """Create or update a MAC address object for a VM interface (NetBox 4.x style)."""
    try:
        # Search for existing MAC using raw API
        results = nb_get('dcim/mac-addresses', params={
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': interface['id']
        }, only=['id', 'mac_address', 'assigned_object_type', 'assigned_object_id'])
        existing = results.get('results', [])

        if existing:
            mac_obj = existing[0]
            if str(mac_obj['mac_address']).upper() != mac.upper():
                # Update using raw PATCH
                nb_patch(f'dcim/mac-addresses/{mac_obj["id"]}', {'mac_address': mac})
                mac_obj['mac_address'] = mac
                print(f"      ✓ Updated MAC: {mac}")
            else:
                print(f"      ✓ MAC unchanged: {mac}")
        else:
            # Create using raw POST
            mac_obj = nb_post('dcim/mac-addresses', {
                'mac_address': mac,
                'assigned_object_type': 'virtualization.vminterface',
                'assigned_object_id': interface['id']
            })
            print(f"      ✓ Created MAC: {mac}")

        # Set as primary MAC on the interface using raw PATCH
        nb_patch(f'virtualization/interfaces/{interface["id"]}', {'primary_mac_address': mac_obj['id']})

    except Exception as e:
        print(f"      ✗ Error syncing MAC {mac}: {e}")


def run_command_qemu_agent(node_name, vm_id, command_args):
    """Run a command inside a QEMU VM via guest agent; return stdout or None."""
    try:
        result = proxmox.nodes(node_name).qemu(vm_id).agent.exec.post(command=command_args)
        pid = result.get('pid')
        if not pid:
            return None
        for _ in range(15):
            time.sleep(1)
            status = proxmox.nodes(node_name).qemu(vm_id).agent('exec-status').get(pid=pid)
            if status.get('exited'):
                return status.get('out-data', '')
        return None
    except Exception:
        return None



def parse_open_ports(ss_output):
    """Parse `ss -tlnp` output into {service_name: [port, ...]} dict.

    Handles two formats:
      Old (with Netid):  tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(...)
      New (TCP-only -t): LISTEN 0 4096 *:80 *:* users:(...)
    """
    ports_by_name = {}
    for line in ss_output.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        # Detect format: old has 'tcp' as first token, new starts with 'LISTEN'
        if parts[0] == 'tcp' and len(parts) >= 6 and parts[1] == 'LISTEN':
            local_addr, proc_col_idx = parts[4], 6
        elif parts[0] == 'LISTEN' and len(parts) >= 5:
            local_addr, proc_col_idx = parts[3], 5
        else:
            continue
        try:
            port = int(local_addr.rsplit(':', 1)[-1])
        except ValueError:
            continue
        name = 'unknown'
        if len(parts) > proc_col_idx:
            m = re.search(r'"([^"]+)"', parts[proc_col_idx])
            if m:
                name = m.group(1)
        ports_by_name.setdefault(name, []).append(port)
    return ports_by_name


def get_open_tcp_ports(node_name, vm_id, vm_type):
    """Return open TCP ports for a running QEMU VM as {name: [ports]} dict. LXC not supported."""
    if vm_type != 'qemu':
        return {}
    output = run_command_qemu_agent(node_name, vm_id, ['ss', '-tlnp'])
    if not output:
        return {}
    return parse_open_ports(output)


def sync_services(vm, ports_by_name):
    """Create/update TCP service records in NetBox for a VM."""
    # Get existing services using raw API
    results = nb_get('ipam/services', params={
        'parent_object_type': 'virtualization.virtualmachine',
        'parent_object_id': vm['id'],
        'protocol': 'tcp'
    }, only=['id', 'name', 'ports', 'protocol', 'parent_object_type', 'parent_object_id'])
    existing_services = {s['name']: s for s in results.get('results', [])}

    for name, ports in ports_by_name.items():
        ports = sorted(set(ports))
        if name in existing_services:
            svc = existing_services[name]
            if sorted(svc.get('ports', []) or []) != ports:
                # Update using raw PATCH
                nb_patch(f'ipam/services/{svc["id"]}', {'ports': ports})
                svc['ports'] = ports
                print(f"      ✓ Updated service: {name} -> {ports}")
            else:
                print(f"      - Service unchanged: {name} {ports}")
        else:
            # Create using raw POST
            nb_post('ipam/services', {
                'name': name,
                'protocol': 'tcp',
                'ports': ports,
                'parent_object_type': 'virtualization.virtualmachine',
                'parent_object_id': vm['id'],
            })
            print(f"      ✓ Created service: {name} -> {ports}")



def assign_ip_to_interface(ip_address, interface, vm):
    """Assign an IP address to an interface in NetBox"""
    try:
        # Check if IP already exists using raw API
        results = nb_get('ipam/ip-addresses', params={'address': ip_address}, 
                        only=['id', 'address', 'assigned_object_type', 'assigned_object_id', 'status'])
        existing_ip = results.get('results', [{}])[0] if results.get('count', 0) > 0 else None
        
        ip_data = {
            'address': ip_address,
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': interface['id'],
            'status': 'active'
        }
        
        if existing_ip:
            # Update using raw PATCH
            nb_patch(f'ipam/ip-addresses/{existing_ip["id"]}', ip_data)
            print(f"        ✓ Updated IP: {ip_address}")
        else:
            # Create using raw POST
            nb_post('ipam/ip-addresses', ip_data)
            print(f"        ✓ Created IP: {ip_address}")
    
    except Exception as e:
        print(f"        ✗ Error assigning IP {ip_address}: {e}")

if __name__ == "__main__":
    print("=" * 60)
    print("Starting Proxmox to NetBox sync...")
    print("=" * 60)
    try:
        sync_vms()
        print("\n" + "=" * 60)
        print("Sync completed successfully!")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"Sync failed with error: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
