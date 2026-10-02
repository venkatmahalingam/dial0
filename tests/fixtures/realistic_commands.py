"""A realistic slice of the SONiC command tree (paths + help, approximated from the SONiC Command Reference),
used to test search ranking at real-world scale. The small fixture tree can't catch ranking problems:
with hundreds of commands, many share words like 'ip', 'vlan' or 'address'."""
COMMANDS = [
    # vlan
    ("config vlan add", "Add VLAN", "<vid>"), ("config vlan del", "Delete VLAN", "<vid>"),
    ("config vlan member add", "Add VLAN member", "[-u|--untagged] <vid> <port>"),
    ("config vlan member del", "Delete VLAN member", "<vid> <port>"),
    ("config vlan dhcp_relay add", "Add a destination IP address to the VLAN's DHCP relay", "<vid> <dhcp_relay_destination_ips>"),
    ("config vlan dhcp_relay del", "Remove a destination IP address from the VLAN's DHCP relay", "<vid> <dhcp_relay_destination_ips>"),
    ("config vlan proxy_arp", "Enable/disable proxy ARP on a VLAN interface", "<vid> <mode>"),
    ("show vlan brief", "Show all bridge information", "[--verbose]"),
    ("show vlan config", "Show VLAN configuration", ""),
    ("show ip helper-address config", "Show DHCP relay helper address configuration", "[vlan]"),
    ("show ip helper-address statistics", "Show DHCP relay helper address statistics", "[vlan]"),
    ("show dhcp_relay ipv4 helper", "Show DHCPv4 relay helper", ""),
    ("show dhcp_relay ipv6 destination", "Show DHCPv6 relay destinations", ""),
    # interfaces
    ("config interface ip add", "Add an IP address towards the interface", "<interface_name> <ip_addr> [gw]"),
    ("config interface ip remove", "Remove an IP address from the interface", "<interface_name> <ip_addr>"),
    ("config interface ip loopback-action", "Set IP interface loopback action", "<interface_name> <action>"),
    ("config interface startup", "Start up interface", "<interface_name>"),
    ("config interface shutdown", "Shut down interface", "<interface_name>"),
    ("config interface mtu", "Set interface mtu", "<interface_name> <interface_mtu>"),
    ("config interface speed", "Set interface speed", "<interface_name> <interface_speed>"),
    ("config interface fec", "Set interface fec", "<interface_name> <interface_fec>"),
    ("config interface autoneg", "Set interface auto negotiation mode", "<interface_name> <mode>"),
    ("config interface description", "Set interface description", "<interface_name> <description>"),
    ("config interface breakout", "Set interface breakout mode", "<interface_name> <mode>"),
    ("config interface vrf bind", "Bind the interface to VRF", "<interface_name> <vrf_name>"),
    ("config interface vrf unbind", "Unbind the interface from VRF", "<interface_name>"),
    ("config interface transceiver lpmode", "Enable/disable low-power mode for SFP transceiver", "<interface_name> <state>"),
    ("config interface transceiver reset", "Reset SFP transceiver", "<interface_name>"),
    ("config interface ipv6 enable use-link-local-only", "Enable IPv6 link local address on interface", "<interface_name>"),
    ("show interfaces status", "Show Interface status information", "[interfacename]"),
    ("show interfaces description", "Show interface status, protocol and description", "[interfacename]"),
    ("show interfaces counters", "Show interface counters", "[-i interface]"),
    ("show interfaces counters errors", "Show interface counters errors", ""),
    ("show interfaces counters rates", "Show interface counters rates", ""),
    ("show interfaces transceiver eeprom", "Show interface transceiver EEPROM information", "[interfacename]"),
    ("show interfaces transceiver presence", "Show interface transceiver presence", "[interfacename]"),
    ("show interfaces portchannel", "Show PortChannel information", ""),
    ("show interfaces naming_mode", "Show interface naming_mode status", ""),
    ("show interfaces neighbor expected", "Show expected neighbor information by interfaces", ""),
    ("show ip interfaces", "Show interfaces IPv4 address", ""),
    ("show ipv6 interfaces", "Show interfaces IPv6 address", ""),
    # portchannel / loopback / vrf / routes
    ("config portchannel add", "Add PortChannel", "<portchannel_name> [--min-links N] [--fallback true|false]"),
    ("config portchannel del", "Remove PortChannel", "<portchannel_name>"),
    ("config portchannel member add", "Add member to port channel", "<portchannel_name> <port_name>"),
    ("config portchannel member del", "Remove member from portchannel", "<portchannel_name> <port_name>"),
    ("config loopback add", "Add loopback interface", "<loopback_name>"),
    ("config loopback del", "Delete loopback interface", "<loopback_name>"),
    ("config vrf add", "Add vrf", "<vrf_name>"), ("config vrf del", "Del vrf", "<vrf_name>"),
    ("config route add", "Add route command", "prefix [vrf <vrf_name>] <A.B.C.D/M> nexthop <A.B.C.D>"),
    ("config route del", "Del route command", "prefix [vrf <vrf_name>] <A.B.C.D/M> nexthop <A.B.C.D>"),
    ("show ip route", "Show IP (IPv4) routing table", "[ip_address] [vrf <vrf_name>]"),
    ("show ipv6 route", "Show IPv6 routing table", ""),
    ("show vrf", "Show vrf config", "[vrf_name]"),
    ("show ip prefix-list", "Show IPv4 prefix lists", ""),
    ("show ipv6 prefix-list", "Show IPv6 prefix lists", ""),
    ("show ip protocol", "Show IPv4 protocol information", ""),
    # bgp
    ("show ip bgp summary", "Show summarized information of IPv4 BGP state", ""),
    ("show ip bgp neighbors", "Show IP (IPv4) BGP neighbors", "[ipaddress] [info_type]"),
    ("show ip bgp network", "Show BGP ipv4 network", "[ipaddress]"),
    ("show ipv6 bgp summary", "Show summarized information of IPv6 BGP state", ""),
    ("config bgp shutdown all", "Shut down all BGP sessions", ""),
    ("config bgp shutdown neighbor", "Shut down BGP session by neighbor IP address or hostname", "<ipaddr_or_hostname>"),
    ("config bgp startup all", "Start up all BGP sessions", ""),
    ("config bgp startup neighbor", "Start up BGP session by neighbor IP address or hostname", "<ipaddr_or_hostname>"),
    ("config bgp remove neighbor", "Deletes BGP neighbor configuration of given hostname or ip", "<neighbor_ip_or_hostname>"),
    # mac / arp / lldp / ndp
    ("show mac", "Show MAC (FDB) entries", "[-v vlan] [-p port]"),
    ("show mac aging-time", "Show MAC aging time", ""),
    ("config mac add", "Add static MAC address", "<mac> <vlan> <interface>"),
    ("config mac del", "Delete static MAC address", "<mac> <vlan>"),
    ("sonic-clear fdb all", "Clear all FDB entries", ""),
    ("show arp", "Show IP ARP table", "[ipaddress] [-if iface]"),
    ("show ndp", "Show IPv6 Neighbour table", "[ip6address] [-if iface]"),
    ("show lldp table", "Show LLDP neighbors in tabular format", ""),
    ("show lldp neighbors", "Show LLDP neighbors", "[interfacename]"),
    # system
    ("show version", "Show version information", ""), ("show uptime", "Show system uptime", ""),
    ("show platform summary", "Show hardware platform information", ""),
    ("show platform syseeprom", "Show system EEPROM information", ""),
    ("show platform psustatus", "Show PSU status information", ""),
    ("show platform fan", "Show fan status information", ""),
    ("show platform temperature", "Show device temperature information", ""),
    ("show environment", "Show environmentals (voltages, fans, temps)", ""),
    ("show processes cpu", "Show processes CPU info", ""), ("show processes memory", "Show processes memory info", ""),
    ("show system-memory", "Show memory information", ""),
    ("show system-health summary", "Show system-health summary information", ""),
    ("show system-health detail", "Show system-health detail information", ""),
    ("show system-health monitor-list", "Show system-health monitored services and devices name list", ""),
    ("show cores list", "List available coredump files", ""), ("show cores info", "Display information about a core file", ""),
    ("show feature status", "Show feature status", "[feature_name]"),
    ("config feature state", "Configure status of feature", "<feature_name> <state>"),
    ("show services", "Show all daemon services", ""),
    ("show reboot-cause", "Show cause of most recent reboot", ""),
    ("show logging", "Show system log", "[process] [-l lines] [-f]"),
    ("show techsupport", "Gather information for troubleshooting", ""),
    ("show runningconfiguration all", "Show full running configuration", ""),
    ("show runningconfiguration interfaces", "Show interfaces running configuration", ""),
    ("show startupconfiguration bgp", "Show BGP startup configuration", ""),
    ("show clock", "Show date and time", ""), ("show ntp", "Show NTP information", ""),
    ("config ntp add", "Add NTP server", "<ntp_ip_address>"), ("config ntp del", "Delete NTP server", "<ntp_ip_address>"),
    ("config save", "Export current config DB to a file on disk", "[-y] [filename]"),
    ("config load", "Import a previous saved config DB dump file", "[-y] [filename]"),
    ("config hostname", "Change device hostname without impacting the traffic", "<new_hostname>"),
    ("config syslog add", "Add syslog server IP", "<syslog_ip_address>"),
    ("config syslog del", "Delete syslog server IP", "<syslog_ip_address>"),
    ("config snmp community add", "Add snmp community", "<community> <type>"),
    ("config snmp community del", "Delete snmp community", "<community>"),
    ("config snmpagentaddress add", "Add SNMP agent listening IP address", "<agentip>"),
    ("show snmpagentaddress", "Show SNMP agent listening IP address configuration", ""),
    ("config aaa authentication login", "Switch login authentication", "<auth_protocol>"),
    ("config tacacs add", "Specify a TACACS+ server", "<ip_address>"),
    ("config ldap-server add", "Add LDAP server", "<address>"),
    ("show ldap-server", "Show LDAP server configuration", ""),
    ("config acl add table", "Add ACL table", "<table_name> <table_type>"),
    ("config acl remove table", "Remove ACL table", "<table_name>"),
    ("config acl update full", "Full update of ACL rules configuration", "<file_name>"),
    ("show acl table", "Show existing ACL tables", "[table_name]"),
    ("show acl rule", "Show existing ACL rules", "[table_name] [rule_id]"),
    ("config hardware access-list", "Configure hardware ACL", ""),
    ("show pfc counters", "Show pfc counters", ""), ("show queue counters", "Show queue counters", "[interfacename]"),
    ("show buffer_pool", "Show buffer pools", ""),
    ("config mgmt-vrf enable", "Enable management VRF", ""), ("config mgmt-vrf disable", "Disable management VRF", ""),
    ("show mgmt-vrf", "Show management VRF attributes", ""),
    ("config kdump enable", "Enable kdump operation", ""), ("show kdump config", "Show kdump configuration", ""),
    ("config vxlan add", "Add VXLAN", "<vxlan_name> <src_ip>"),
    ("config vxlan map add", "Add VLAN-VNI map entry", "<vxlan_name> <vlan_id> <vni>"),
    ("show vxlan tunnel", "Show vxlan tunnel information", ""),
    ("config nat add static basic", "Add static NAT entry", "<global_ip> <local_ip>"),
    ("show nat translations", "Show NAT translations", ""),
    ("config sflow enable", "Enable sFlow", ""), ("show sflow", "Show sFlow information", ""),
    ("config watermark telemetry interval", "Set watermark telemetry interval", "<interval>"),
    ("config warm_restart enable", "Enable warm restart", "[module]"),
    ("show warm_restart state", "Show warm restart state", ""),
    ("config interface counters", "Set interface counters", ""),
]


def build_index():
    """-> commands.json-style nodes (groups created for every prefix)."""
    nodes = {}
    for path, help_, argspec in COMMANDS:
        words = path.split()
        for i in range(1, len(words)):
            g = " ".join(words[:i])
            n = nodes.setdefault(g, {"kind": "group", "help": "", "usage": g, "options": [], "args": [],
                                     "opts_complete": False, "source": "fixture", "children": []})
            n["kind"] = "group"
            if words[i] not in n["children"]:
                n["children"].append(words[i])
        prev = nodes.get(path)
        nodes[path] = {"kind": "command", "help": help_, "usage": (path + " " + argspec).strip(), "options": [],
                       "args": [], "opts_complete": False, "source": "fixture", "children": []}
        if prev:  # e.g. "show interfaces counters" and "show interfaces counters errors"
            nodes[path].update(kind="group", runnable=True, children=prev["children"])
    for path, n in nodes.items():  # commands listed before their subcommands also become runnable groups
        if n["kind"] == "group" and path in {c[0] for c in COMMANDS}:
            n["runnable"] = True
            n["help"] = n["help"] or next(h for c, h, _ in COMMANDS if c == path)
            n["usage"] = next((c + " " + a).strip() for c, _, a in COMMANDS if c == path)
    return nodes
