
#!/usr/bin/env python3
"""
wifi_scan_and_lan_discovery.py

Features:
- Scan nearby Wi-Fi networks on Windows and Linux
- Select a Wi-Fi network by number
- Discover responsive devices on the local subnet
- Display device name, IPv4, MAC address, and vendor

Optional installation:
python -m pip install mac-vendor-lookup
"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional
from shutil import which


@dataclass
class WifiNetwork:
    ssid: str = ""
    bssid: str = ""
    signal: str = ""
    channel: str = ""
    security: str = ""
    raw: str = ""


@dataclass
class Device:
    name: str
    ipv4: str
    mac: str
    vendor: str = ""


def run_cmd(cmd: List[str]) -> str:
    return subprocess.check_output(
        cmd, text=True, stderr=subprocess.STDOUT
    )


def scan_wifi_windows() -> List[WifiNetwork]:
    output = run_cmd(
        ["netsh", "wlan", "show", "networks", "mode=bssid"]
    )

    networks: List[WifiNetwork] = []
    current: Optional[WifiNetwork] = None

    for line in output.splitlines():
        line = line.strip()

        match = re.match(r"SSID\s+\d+\s*:\s*(.*)", line)
        if match:
            if current and current.ssid:
                networks.append(current)

            current = WifiNetwork(ssid=match.group(1).strip())
            continue

        if not current:
            continue

        patterns = [
            (r"BSSID\s+\d+\s*:\s*(.*)", "bssid"),
            (r"Signal\s*:\s*(.*)", "signal"),
            (r"Channel\s*:\s*(.*)", "channel"),
            (r"Authentication\s*:\s*(.*)", "security"),
        ]

        for pattern, field in patterns:
            match = re.match(pattern, line)
            if match:
                setattr(current, field, match.group(1).strip())
                break

    if current and current.ssid:
        networks.append(current)

    return networks


def scan_wifi_linux() -> List[WifiNetwork]:
    if not which("nmcli"):
        raise RuntimeError("nmcli is not installed or not in PATH.")

    output = run_cmd([
        "nmcli", "-t",
        "-f", "SSID,BSSID,SIGNAL,CHAN,SECURITY",
        "dev", "wifi", "list"
    ])

    networks = []

    for line in output.splitlines():
        parts = line.split(":")
        if len(parts) < 5:
            continue

        ssid, bssid, signal, channel = parts[:4]
        security = ":".join(parts[4:])

        networks.append(WifiNetwork(
            ssid=ssid.replace(r"\:", ":"),
            bssid=bssid,
            signal=signal,
            channel=channel,
            security=security,
            raw=line
        ))

    return networks


def normalize_mac(mac: str) -> str:
    return re.sub(r"[^0-9A-F]", "", mac.upper())


class VendorLookup:
    def __init__(self):
        self.lookup_service = None

        try:
            from mac_vendor_lookup import MacLookup
            self.lookup_service = MacLookup()
        except ImportError:
            pass

    def lookup(self, mac: str) -> str:
        if not mac or mac == "Unknown":
            return "Unknown"

        if self.lookup_service:
            try:
                return self.lookup_service.lookup(mac)
            except Exception:
                pass

        return "Unknown"


def get_local_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def get_local_subnet() -> Optional[ipaddress.IPv4Network]:
    """Find the active local IPv4 subnet using Windows interface data."""
    if platform.system().lower() != "windows":
        return None

    try:
        output = run_cmd(["ipconfig"])
        local_ip = get_local_ip()

        if not local_ip:
            return None

        for block in output.split("\n\n"):
            if local_ip not in block:
                continue

            mask_match = re.search(
                r"(?:Subnet Mask|Subnet mask)[ .:]*([0-9.]+)",
                block
            )

            if mask_match:
                mask = mask_match.group(1)
                interface = ipaddress.IPv4Interface(
                    f"{local_ip}/{mask}"
                )
                return interface.network

    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return None


def ping_host(ip: str, timeout_ms: int = 800) -> bool:
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ping_sweep(subnet: str, workers: int = 64) -> List[str]:
    network = ipaddress.ip_network(subnet, strict=False)

    if network.num_addresses > 1024:
        raise ValueError(
            "Subnet too large for this simple scanner."
        )

    alive = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(ping_host, str(ip)): str(ip)
            for ip in network.hosts()
        }

        for future in as_completed(futures):
            ip = futures[future]

            try:
                if future.result():
                    alive.append(ip)
            except Exception:
                pass

    return sorted(alive, key=ipaddress.ip_address)


def get_mac_from_system_table(ip: str) -> str:
    try:
        output = run_cmd(["arp", "-a", ip])

        match = re.search(
            r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})",
            output
        )

        if match:
            return match.group(1).replace("-", ":").lower()

    except (OSError, subprocess.SubprocessError):
        pass

    return "Unknown"


def get_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror):
        return "Unknown"


def discover_devices(
    subnet: str,
    vendor_lookup: VendorLookup
) -> List[Device]:
    devices = []

    for ip in ping_sweep(subnet):
        mac = get_mac_from_system_table(ip)

        devices.append(Device(
            name=get_hostname(ip),
            ipv4=ip,
            mac=mac,
            vendor=vendor_lookup.lookup(mac)
        ))

    return devices


def print_wifi_networks(networks: List[WifiNetwork]):
    print("\nNearby Wi-Fi Networks")
    print("=" * 65)

    for i, network in enumerate(networks, 1):
        print(f"{i}. SSID: {network.ssid or '<hidden>'}")
        print(f"   BSSID: {network.bssid}")
        print(f"   Signal: {network.signal}")
        print(f"   Channel: {network.channel}")
        print(f"   Security: {network.security}")
        print("-" * 65)


def print_devices(devices: List[Device]):
    print("\nDevices Found on Local Network")
    print("=" * 100)
    print(f"{'No.':<5}{'Name':<30}{'IPv4':<18}{'MAC':<22}{'Vendor'}")
    print("-" * 100)

    for i, device in enumerate(devices, 1):
        print(
            f"{i:<5}{device.name[:28]:<30}"
            f"{device.ipv4:<18}{device.mac:<22}{device.vendor}"
        )


def main():
    print("Wi-Fi Scanner and Local Network Discovery")
    print("Use only on networks you own or have permission to inspect.")

    try:
        if platform.system().lower() == "windows":
            networks = scan_wifi_windows()
        elif platform.system().lower() == "linux":
            networks = scan_wifi_linux()
        else:
            print("Unsupported operating system.")
            return

        if not networks:
            print("No Wi-Fi networks found.")
            return

        print_wifi_networks(networks)

        choice = input("Pick a Wi-Fi number: ").strip()

        if not choice.isdigit():
            print("Invalid selection.")
            return

        index = int(choice)

        if index < 1 or index > len(networks):
            print("Selection out of range.")
            return

        selected = networks[index - 1]
        print(f"\nSelected Wi-Fi: {selected.ssid or '<hidden>'}")
        print("Note: selecting a network does not connect to it.")

        subnet = get_local_subnet()

        if not subnet:
            print("Could not detect the local subnet.")
            print("Check your active Wi-Fi connection and IP settings.")
            return

        print(f"Checking local subnet: {subnet}")
        print("Searching for devices that respond to ping...")

        devices = discover_devices(str(subnet), VendorLookup())
        print_devices(devices)

    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}")


if __name__ == "__main__":
    main()
