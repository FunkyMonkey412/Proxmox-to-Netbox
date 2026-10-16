#!/usr/bin/env python3
"""
Proxmox to NetBox Sync Script
Save as: /opt/scripts/proxmox-netbox-sync.py

Copy .env.example to .env and fill in your credentials before running.
"""

from proxmoxer import ProxmoxAPI
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

# Setup NetBox session
session = requests.Session()
session.verify = False

# Cache for NetBox objects to avoid repeated API calls
_NB_CACHE = {}


def get_nb_cache(key, fetch_func):
    """Get from cache or fetch and cache."""
    if key not in _NB_CACHE:
        _NB_CACHE[key] = fetch_func()
    return _NB_CACHE[key]


def clear_nb_cache():
    """Clear the NetBox cache."""
    global _NB_CACHE
    _NB_CACHE = {}




def nb_api(method, endpoint, data=None, params=None):
    """Raw API request to NetBox."""
    url = f"{NETBOX_URL}/api/{endpoint}/"
    headers = {"Authorization": f"Token {NETBOX_TOKEN}", "Content-Type": "application/json"}
    
    # Always disable SSL verification for self-signed certs
    verify_ssl = False
    
    if method == 'GET':
        response = session.get(url, headers=headers, params=params, verify=verify_ssl)
    elif method == 'POST':
        response = session.post(url, headers=headers, json=data, verify=verify_ssl)
    elif method == 'PATCH':
        # For PATCH, endpoint should be like 'virtualization/virtual-machines' 
        # and data should contain 'id'
        obj_id = data.get('id') if data and isinstance(data, dict) else None
        if obj_id and '{' not in endpoint and '/' not in endpoint.split('/')[-1]:
            # endpoint doesn't already have an ID, append it
            url = f"{NETBOX_URL}/api/{endpoint}/{obj_id}/"
        response = session.patch(url, headers=headers, json=data, verify=verify_ssl)
    else:
        raise ValueError(f"Unsupported method: {method}")
    
    response.raise_for_status()
    return response.json() if response.content else None


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
    # Fetch all clusters and filter locally
    all_clusters = nb_api('GET', 'virtualization/clusters')
    matching = [c for c in all_clusters.get('results', []) if c.get('name') == CLUSTER_NAME]
    if matching:
        cluster = matching[0]
        print(f"Found existing cluster: {CLUSTER_NAME}")
        return cluster

    # Create cluster type if needed
    cluster_type_slug = CLUSTER_TYPE_NAME.lower().replace(" ", "-")
    all_types = nb_api('GET', 'virtualization/cluster-types')
    matching_types = [t for t in all_types.get('results', []) if t.get('name') == CLUSTER_TYPE_NAME]
    
    if matching_types:
        cluster_type = matching_types[0]
        print(f"Found existing cluster type: {CLUSTER_TYPE_NAME}")
    else:
        print(f"Creating cluster type: {CLUSTER_TYPE_NAME}")
        cluster_type = nb_api('POST', 'virtualization/cluster-types', data={
            'name': CLUSTER_TYPE_NAME,
            'slug': cluster_type_slug
        })
    
    # Create cluster
    print(f"Creating cluster: {CLUSTER_NAME}")
    cluster = nb_api('POST', 'virtualization/clusters', data={
        'name': CLUSTER_NAME,
        'type': cluster_type['id']
    })
    return cluster


def get_vm_ip_addresses(node_name, vm_id, vm_type='qemu'):
    """Get IP addresses from VM using QEMU guest agent or LXC"""
    ip_addresses = []
    
    try:
        if vm_type == 'qemu':
            network_data = proxmox.nodes(node_name).qemu(vm_id).agent.get('network-get-interfaces')
            
            if network_data and 'result' in network_data:
                for interface in network_data['result']:
                    if 'ip-addresses' in interface:
                        for ip_info in interface['ip-addresses']:
                            ip_address = ip_info.get('ip-address')
                            ip_type = ip_info.get('ip-address-type')
                            if not ip_address:
                                continue
                            if ip_address.startswith('127.') or ip_address == '::1':
                                continue
                            if ip_address.startswith('169.254.') or ip_address.startswith('fe80:'):
                                continue
                            if interface.get('name', '').startswith('lo'):
                                continue
                            prefix = ip_info.get('prefix', 24 if ip_type == 'ipv4' else 64)
                            ip_addresses.append({
                                'address': f"{ip_address}/{prefix}",
                                'interface': interface.get('name', 'unknown')
                            })
        
        elif vm_type == 'lxc':
            network_data = proxmox.nodes(node_name).lxc(vm_id).interfaces.get()
            for interface in network_data:
                if interface.get('name', '').startswith('lo'):
                    continue
                if 'inet' in interface:
                    ip = interface['inet'].split('/')[0]
                    if not ip.startswith('127.') and not ip.startswith('169.254.'):
                        ip_addresses.append({
                            'address': interface['inet'],
                            'interface': interface.get('name', 'eth0')
                        })
                if 'inet6' in interface:
                    ip = interface['inet6'].split('/')[0]
                    if ip != '::1' and not ip.startswith('fe80:'):
                        ip_addresses.append({
                            'address': interface['inet6'],
                            'interface': interface.get('name', 'eth0')
                        })
    except Exception:
        pass
    
    return ip_addresses


def sync_vms():
    """Sync all VMs from Proxmox to NetBox"""
    try:
        cluster = get_or_create_cluster()
    except Exception as e:
        print(f"Could not get/create cluster in NetBox: {e}")
        return
    
    for node in proxmox.nodes.get():
        node_name = node['node']
        print(f"\nProcessing node: {node_name}")
        
        # Process QEMU VMs - Proxmox API call
        try:
            vms = proxmox.nodes(node_name).qemu.get()
        except Exception as e:
            print(f"Could not get VMs from {node_name}: {e}")
            vms = []
        
        for vm in vms:
            try:
                process_vm(node_name, vm, cluster, 'qemu')
            except Exception as e:
                print(f"Error processing VM {vm.get('name', 'unknown')}: {e}")
        
        # Process LXC Containers - Proxmox API call
        try:
            containers = proxmox.nodes(node_name).lxc.get()
        except Exception as e:
            print(f"Could not get containers from {node_name}: {e}")
            containers = []
        
        for container in containers:
            try:
                process_vm(node_name, container, cluster, 'lxc')
            except Exception as e:
                print(f"Error processing container {container.get('name', 'unknown')}: {e}")


def process_vm(node_name, vm, cluster, vm_type='qemu'):
    """Process a single VM or container"""
    vm_name = vm['name']
    vm_id = vm['vmid']
    status = 'active' if vm['status'] == 'running' else 'offline'
    
    type_label = "Container" if vm_type == 'lxc' else "VM"
    print(f"  Processing {type_label}: {vm_name} (ID: {vm_id})")
    
    try:
        if vm_type == 'qemu':
            vm_config = proxmox.nodes(node_name).qemu(vm_id).config.get()
        else:
            vm_config = proxmox.nodes(node_name).lxc(vm_id).config.get()
    except Exception as e:
        print(f"    Could not get config for {vm_name}: {e}")
        return
    
    if vm_type == 'qemu':
        vcpus = vm_config.get('cores', 1) * vm_config.get('sockets', 1)
        memory = vm_config.get('memory', 0)
    else:
        vcpus = vm_config.get('cores', 1)
        memory = vm_config.get('memory', 0)
    
    # Fetch all VMs from cache and filter locally by name
    all_vms = get_nb_cache('all_vms', lambda: nb_api('GET', 'virtualization/virtual-machines'))
    matching_vms = [v for v in all_vms.get('results', []) if v.get('name') == vm_name]
    existing_vm = matching_vms[0] if matching_vms else None
    
    vm_data = {
        'name': vm_name,
        'cluster': cluster['id'],
        'status': status,
        'vcpus': vcpus,
        'memory': memory,
        'comments': f'Proxmox {type_label} ID: {vm_id}\nNode: {node_name}\nType: {vm_type}'
    }
    
    try:
        if existing_vm:
            nb_api('PATCH', 'virtualization/virtual-machines', data=dict(vm_data, id=existing_vm['id']))
            existing_vm.update(vm_data)
            print(f"    \u2713 Updated {type_label}: {vm_name}")
        else:
            existing_vm = nb_api('POST', 'virtualization/virtual-machines', data=vm_data)
            print(f"    \u2713 Created {type_label}: {vm_name}")
    except Exception as e:
        print(f"    \u2717 Error with {type_label} {vm_name} (NetBox API): {e}")
        return
    
    sync_vm_interfaces(node_name, vm_id, vm_name, vm_type, status, existing_vm)

    if status == 'active':
        print(f"    Discovering open TCP ports on {vm_name}...")
        ports = get_open_tcp_ports(node_name, vm_id, vm_type)
        if ports:
            print(f"      Found services: {{k: v for k, v in ports.items()}}")
        else:
            print(f"      No open ports found (agent unavailable or none listening)")
        sync_services(existing_vm, ports)


def sync_vm_interfaces(node_name, vm_id, vm_name, vm_type='qemu', vm_status='offline', vm_obj=None):
    """Sync VM network interfaces and IP addresses"""
    try:
        if vm_type == 'qemu':
            vm_config = proxmox.nodes(node_name).qemu(vm_id).config.get()
        else:
            vm_config = proxmox.nodes(node_name).lxc(vm_id).config.get()
        
        if not vm_obj:
            return
        
        ip_data = []
        if vm_status == 'active':
            ip_data = get_vm_ip_addresses(node_name, vm_id, vm_type)
            if ip_data:
                print(f"      Found {len(ip_data)} IP address(es)")
        
        for key, value in vm_config.items():
            if key.startswith('net'):
                mac = parse_mac_from_config(value, vm_type)
                interface_name = key
                
                # Fetch all interfaces for this VM and filter locally
                cache_key = f'interfaces_{vm_obj["id"]}'
                all_interfaces = get_nb_cache(cache_key, lambda: nb_api('GET', 'virtualization/interfaces', params={'virtual_machine_id': vm_obj['id']}))
                matching_interfaces = [i for i in all_interfaces.get('results', []) if i.get('name') == interface_name]
                existing_int = matching_interfaces[0] if matching_interfaces else None
                
                int_data = {
                    'virtual_machine': vm_obj['id'],
                    'name': interface_name,
                    'type': 'virtual'
                }

                try:
                    if existing_int:
                        nb_api('PATCH', 'virtualization/interfaces', data=dict(int_data, id=existing_int['id']))
                        interface = dict(existing_int, **int_data)
                        print(f"      \u2713 Updated interface: {interface_name}")
                    else:
                        interface = nb_api('POST', 'virtualization/interfaces', data=int_data)
                        print(f"      \u2713 Created interface: {interface_name}")

                    if mac:
                        sync_mac_address(mac, interface)

                    if ip_data:
                        for ip_info in ip_data:
                            assign_ip_to_interface(ip_info['address'], interface, vm_obj)

                except Exception as e:
                    print(f"      \u2717 Error with interface {interface_name}: {e}")
                
    except Exception as e:
        print(f"    Error syncing interfaces: {e}")


def sync_mac_address(mac, interface):
    """Create or update a MAC address object for a VM interface (NetBox 4.x style)."""
    try:
        all_macs = nb_api('GET', 'dcim/mac-addresses', params={
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': interface['id']
        })
        existing = all_macs.get('results', []) if all_macs else []

        if existing:
            mac_obj = existing[0]
            if str(mac_obj['mac_address']).upper() != mac.upper():
                nb_api('PATCH', 'dcim/mac-addresses', data=dict(mac_address=mac, id=mac_obj['id']))
                mac_obj['mac_address'] = mac
                print(f"      \u2713 Updated MAC: {mac}")
            else:
                print(f"      \u2713 MAC unchanged: {mac}")
        else:
            mac_obj = nb_api('POST', 'dcim/mac-addresses', data={
                'mac_address': mac,
                'assigned_object_type': 'virtualization.vminterface',
                'assigned_object_id': interface['id']
            })
            print(f"      \u2713 Created MAC: {mac}")

        nb_api('PATCH', 'virtualization/interfaces', data=dict(primary_mac_address=mac_obj['id'], id=interface['id']))

    except Exception as e:
        print(f"      \u2717 Error syncing MAC {mac}: {e}")


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
    """Parse ss -tlnp output into {port: [port]} dict.
    Uses port number as key for stable service names across runs."""
    ports_by_name = {}
    for line in ss_output.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
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
        # Use port number as name for stability
        name = f'port-{port}'
        ports_by_name.setdefault(name, []).append(port)
    return ports_by_name


def get_open_tcp_ports(node_name, vm_id, vm_type):
    """Return open TCP ports for a running QEMU VM as {name: [ports]} dict."""
    if vm_type != 'qemu':
        return {}
    output = run_command_qemu_agent(node_name, vm_id, ['ss', '-tlnp'])
    if not output:
        return {}
    return parse_open_ports(output)


def sync_services(vm, ports_by_name):
    """Create/update TCP service records in NetBox for a VM."""
    # Fetch all services from cache and filter locally
    all_services = get_nb_cache('all_services', lambda: nb_api('GET', 'ipam/services'))
    matching_services = [s for s in all_services.get('results', []) 
                       if s.get('parent_object_type') == 'virtualization.virtualmachine' 
                       and s.get('parent_object_id') == vm['id'] 
                       and s.get('protocol') == 'tcp']
    existing_services = {s['name']: s for s in matching_services}

    for name, ports in ports_by_name.items():
        ports = sorted(set(ports))
        if name in existing_services:
            svc = existing_services[name]
            if sorted(svc.get('ports', []) or []) != ports:
                nb_api('PATCH', 'ipam/services', data=dict(ports=ports, id=svc['id']))
                svc['ports'] = ports
                print(f"      \u2713 Updated service: {name} -> {ports}")
            else:
                print(f"      - Service unchanged: {name} {ports}")
        else:
            nb_api('POST', 'ipam/services', data={
                'name': name,
                'protocol': 'tcp',
                'ports': ports,
                'parent_object_type': 'virtualization.virtualmachine',
                'parent_object_id': vm['id'],
            })
            print(f"      \u2713 Created service: {name} -> {ports}")


def assign_ip_to_interface(ip_address, interface, vm):
    """Assign an IP address to an interface in NetBox"""
    try:
        # Fetch all IPs from cache and filter locally
        all_ips = get_nb_cache('all_ips', lambda: nb_api('GET', 'ipam/ip-addresses'))
        matching_ips = [ip for ip in all_ips.get('results', []) if ip.get('address') == ip_address]
        existing_ip = matching_ips[0] if matching_ips else None
        
        ip_data = {
            'address': ip_address,
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': interface['id'],
            'status': 'active'
        }
        
        if existing_ip:
            nb_api('PATCH', 'ipam/ip-addresses', data=dict(ip_data, id=existing_ip['id']))
            print(f"        \u2713 Updated IP: {ip_address}")
        else:
            nb_api('POST', 'ipam/ip-addresses', data=ip_data)
            print(f"        \u2713 Created IP: {ip_address}")
    except Exception as e:
        print(f"        \u2717 Error assigning IP {ip_address}: {e}")


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
