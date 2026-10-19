"""Wake-on-LAN: switch a device on that is fully off, with a "magic packet".

A device that is completely off (a TV in deep standby, a PC that's shut down or asleep)
can't answer anything — ADB, the WebSocket — but its network card can still listen for
one UDP packet: six 0xFF bytes, then its own MAC address sixteen times. It works only
if the device allows it (on a Sony TV: Settings → Network → Remote start; on a PC: Wake
on LAN in the BIOS and on the network adapter) and only on the same network: routers
don't pass broadcasts, so it can't cross into another router's Wi-Fi (README, "Two
routers at home?").
"""

from __future__ import annotations

import ipaddress
import re
import socket

_MAC = re.compile(r"^([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?"
                  r"([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})$", re.IGNORECASE)


def norm_mac(raw) -> str:
    """'AA-BB-CC-DD-EE-FF' / 'aabbccddeeff' → 'aa:bb:cc:dd:ee:ff'; '' if it isn't one
    (or is the all-zero / broadcast address)."""
    m = _MAC.match(str(raw or "").strip())
    if not m:
        return ""
    mac = ":".join(g.lower() for g in m.groups())
    return "" if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") else mac


def magic_packet(mac: str) -> bytes:
    body = bytes.fromhex(norm_mac(mac).replace(":", ""))
    if len(body) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + body * 16


def targets(near: str = "") -> "list[str]":
    """Where to send it: the local broadcast address, plus the /24 broadcast of the
    device's last known address (some Wi-Fi setups drop 255.255.255.255)."""
    out = ["255.255.255.255"]
    try:
        ip = ipaddress.ip_address(str(near or "").split(":")[0])
        if ip.version == 4 and ip.is_private:
            out.append(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    except ValueError:
        pass
    return out


def send(mac: str, near: str = "", sock_factory=socket.socket) -> "tuple[bool, str]":
    """Send the magic packet for `mac` (to ports 9 and 7, a few times). True means it
    left this PC, never that the device woke — the caller checks that."""
    try:
        packet = magic_packet(mac)
    except ValueError as exc:
        return False, str(exc)
    sent = 0
    last = ""
    with sock_factory(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for addr in targets(near):
                for port in (9, 7):
                    try:
                        s.sendto(packet, (addr, port))
                        sent += 1
                    except OSError as exc:
                        last = str(exc)
    if not sent:
        return False, f"The wake-up packet couldn't be sent ({last or 'no network'})."
    return True, f"Sent a wake-up packet to {norm_mac(mac)}."


def mac_from_ip_link(text: str) -> str:
    """The MAC of the interface that's up, from `ip -o link` output (an Android TV's
    shell): the Ethernet port if it's connected, else Wi-Fi."""
    best = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\d+:\s*([\w.@-]+):\s*<([^>]*)>.*?link/ether\s+([0-9a-f:]{17})", line.strip())
        if not m:
            continue
        name, flags, mac = m.group(1).split("@")[0], m.group(2).split(","), m.group(3)
        if "UP" in flags and "LOWER_UP" in flags and norm_mac(mac):
            best[name] = norm_mac(mac)
    for prefix in ("eth", "wlan"):
        for name, mac in best.items():
            if name.startswith(prefix):
                return mac
    return next(iter(best.values()), "")


# Adapters that aren't a real network card (VMs, WSL, containers, Bluetooth): a magic
# packet to them wakes nothing.
_VIRTUAL = re.compile(r"vethernet|virtualbox|vmware|hyper-v|loopback|bluetooth|docker|"
                      r"vboxnet|^veth|^br-|tailscale|zerotier|wsl", re.IGNORECASE)


def local_macs(psutil_mod=None) -> "list[dict]":
    """This PC's network adapters that a phone could wake it through: [{"name", "mac",
    "ip"}], wired ones first (Wi-Fi rarely supports waking from off), at most four."""
    psutil = psutil_mod
    if psutil is None:
        try:
            import psutil
        except ImportError:
            return []
    out = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if not st or not st.isup or _VIRTUAL.search(name):
            continue
        mac = next((norm_mac(a.address) for a in addrs if a.family == psutil.AF_LINK), "")
        ip = next((a.address for a in addrs if a.family == socket.AF_INET
                   and ipaddress.ip_address(a.address).is_private
                   and not a.address.startswith("100.")), "")
        if mac and ip:
            out.append({"name": name, "mac": mac, "ip": ip})
    out.sort(key=lambda a: bool(re.search(r"wi-?fi|wlan|wireless", a["name"], re.IGNORECASE)))
    return out[:4]
