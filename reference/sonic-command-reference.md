# SONiC CLI Command Reference

Commands from the SONiC Command Reference (sonic-utilities), grouped into two sets:
Set 1 has read-only show commands; Set 2 has config commands that change the switch.
Both sets cover the same areas: Interfaces, VLAN, PortChannel, IP/IPv6 and Services.
Interface names use the SONiC default naming mode (Ethernet0, Ethernet4, ...).
A comma-separated list or range (Ethernet8,Ethernet16-20) targets several interfaces at once.

---

## Set 1: Show Commands

Read-only commands that display the switch's current operational and configured state.
They do not change the configuration, are safe to run at any time, and do not need sudo.
Use them to inspect the switch, troubleshoot, and check the result of config changes.

### Interfaces
Front-panel port state and statistics: admin/oper status, speed, MTU, description,
counters, packet drops (all ports, or only ports with non-zero drops), auto-negotiation,
FEC, TPID, naming mode, switchport mode, transceiver/optics details, and LLDP neighbors
discovered on each port.

```
show interfaces status
show interfaces status Ethernet8,Ethernet168-180
show interface counters
show interfaces counters
show interfaces counters -i Ethernet4,Ethernet12-16
show interfaces counters -p 5
show interfaces counters detailed Ethernet8
show interface pktdrops
show interface pktdrops nonzero
show interfaces fec status
show interfaces tpid
show interfaces naming_mode
show interfaces neighbor expected
show interface transceiver
show interfaces transceiver presence Ethernet100
show interfaces transceiver status Ethernet100
show interfaces transceiver eeprom --dom Ethernet0
show lldp neighbors
show lldp neighbors Ethernet112
```

### VLAN
VLAN configuration and Layer 2 forwarding state: VLAN IDs, member ports and tagging mode,
VLAN IP addresses, proxy ARP, static anycast gateway, DHCP helpers, and the MAC address
(FDB) table filtered by VLAN, port, MAC address or entry type.

```
show vlan brief
show vlan config
show mac
show mac -v 1000
show mac -p Ethernet192
show mac aging-time
```

### PortChannel
Link aggregation (LAG/LACP) state: each PortChannel with its protocol status and member
ports (selected/deselected), plus router-interface counters for a PortChannel.

```
show interfaces portchannel
show interfaces counters rif PortChannel0001
show interfaces counters detailed Ethernet0
show interfaces counters detailed PortChannel0001
show interfaces counters detailed Vlan20
```

### IP / IPv6
Layer 3 state: interface IPv4/IPv6 addresses, routing tables (default and per VRF),
routing protocols, ARP and NDP neighbor tables, VRFs, the management VRF, the static
anycast gateway MAC and the VLAN interfaces that use it, and BGP summary and neighbor
details.

```
show ip interfaces
show ip route
show ip route 10.1.1.0
show ip route vrf Vrf-red
show ip route vrf Vrf-red 11.1.1.1/32
show ip protocol
show ipv6 interfaces
show ipv6 route
show ipv6 route fc00:1::32
show ipv6 protocol
show arp
show arp -if Ethernet40
show arp 192.168.1.181
show ndp
show ndp -if eth0
show vrf
show mgmt-vrf
show mgmt-vrf routes
show ip bgp summary
show ip bgp neighbors
show ip bgp neighbors 192.168.1.161 routes
show ipv6 bgp summary
```

---

## Set 2: Config Commands

Commands that change the switch's running configuration in CONFIG_DB.
They need root privileges, so they are run with sudo.
They can affect traffic, so check the current state with Set 1 first and verify
the result with Set 1 afterwards. Changes apply immediately but are lost on reboot
unless saved with "sudo config save -y".

### Interfaces
Port settings: admin up/down, speed, MTU, auto-negotiation, TPID, port breakout,
and switchport mode (access, trunk or routed).

```
sudo config interface shutdown Ethernet63
sudo config interface shutdown Ethernet8,Ethernet16-20,Ethernet32
sudo config interface startup Ethernet63
sudo config interface startup Ethernet8,Ethernet16-20,Ethernet32
sudo config interface speed Ethernet63 40000
sudo config interface mtu Ethernet64 1500
sudo config interface autoneg Ethernet0 enabled
sudo config interface autoneg Ethernet0 disabled
sudo config interface tpid Ethernet64 0x9200
sudo config interface breakout Ethernet0 4x25G[10G] -f -l -v -y
sudo config switchport mode access Ethernet0
sudo config switchport mode trunk Ethernet4
sudo config switchport mode routed Ethernet12
```

### VLAN
Create and delete VLANs (one at a time, or several with -m as a range or list);
add or remove member ports, which can be Ethernet interfaces or PortChannels
(tagged by default, untagged with -u); -e adds a port to all existing VLANs except
the ones listed, and "all" adds it to every existing VLAN; set proxy ARP per VLAN;
clear the MAC (FDB) table.
A port or PortChannel can be an untagged member of only one VLAN, but a tagged member
of many. A PortChannel added to a VLAN must not have an IP address, and an Ethernet
port that belongs to a PortChannel cannot be added to a VLAN on its own; add the
PortChannel instead.

```
sudo config vlan add 100
sudo config vlan add -m 100-103
sudo config vlan add -m 105,106,107,108
sudo config vlan del 100
sudo config vlan member add 100 Ethernet0        # tagged member: the default, no flag
sudo config vlan member add -u 100 Ethernet4     # untagged member: -u
sudo config vlan member del 100 Ethernet0
sudo config vlan member add 100 PortChannel0011  # tagged member: the default, no flag
sudo config vlan member add -u 200 PortChannel0012  # untagged member: -u
sudo config vlan member del 100 PortChannel0011
sudo config vlan member del 200 PortChannel0012
sudo config vlan proxy_arp 1000 enabled
```

### PortChannel
Create and delete LAGs and manage their members. Names must follow the PortChannelxxxx
format (1-4 digits). Optional settings: --min-links (minimum links needed to bring the LAG
up), --fallback (LACP fallback) and --fast-rate (LACPDUs every 1s instead of every 30s).
A PortChannel can only be deleted after all its members are removed. A port must be
removed from its current PortChannel before it is added to another.

```
sudo config portchannel add PortChannel0011
sudo config portchannel add PortChannel0012 --min-links 2
sudo config portchannel add PortChannel0013 --fallback true
sudo config portchannel add PortChannel0014 --fast-rate true
sudo config portchannel member add PortChannel0011 Ethernet4
sudo config portchannel member del PortChannel0011 Ethernet4
sudo config portchannel del PortChannel0011
```

### System
Switch identity: set the switch's hostname. The name must be a valid hostname (letters, digits and hyphens, not
starting or ending with a hyphen).

```
sudo config hostname leaf1
```

### IP / IPv6
Layer 3 addressing and routing domains: add or remove IP addresses on Ethernet, VLAN,
PortChannel and management (eth0) interfaces; enable or disable IPv6 link-local-only
mode; bind interfaces to or unbind them from a VRF; create loopback interfaces;
create and delete VRFs, including the management VRF; configure a static anycast
gateway (SAG) on VLAN interfaces.
Static anycast gateway lets several switches serve the same gateway IP and MAC for a
VLAN, so hosts keep the same default gateway wherever they connect. To set it up:
(1) set the global anycast gateway MAC with "static-anycast-gateway mac_address add",
(2) assign the anycast gateway IP to the VLAN interface with
"interface ip anycast-address add <vlan_interface> <ip_addr/prefix>",
(3) enable anycast on that VLAN with "vlan static-anycast-gateway enable <vlan_id>".
The same MAC and anycast gateway IP must be configured on every switch that serves
the VLAN. "mac_address del" removes the global anycast MAC.

```
sudo config interface ip add Ethernet63 10.11.12.13/24
sudo config interface ip add Vlan100 10.11.12.13/24
sudo config interface ip add PortChannel0011 10.0.0.1/31
sudo config interface ip add eth0 20.11.12.13/24 20.11.12.254
sudo config interface ip remove Ethernet63 10.11.12.13/24
sudo config interface ip remove Vlan100 10.11.12.13/24
sudo config interface ipv6 enable use-link-local-only Vlan206
sudo config interface ipv6 enable use-link-local-only PortChannel007
sudo config interface ipv6 disable use-link-local-only Ethernet52
sudo config interface ip unnumbered add Ethernet0
sudo config interface vrf bind Ethernet0 Vrf-red
sudo config interface vrf unbind Ethernet0
sudo config loopback add Loopback11
sudo config vrf add mgmt
sudo config vrf del mgmt
sudo config interface ip anycast-address add Vlan100 1.1.1.1/24
```

