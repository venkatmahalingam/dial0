# SONiC COMMAND LINE INTERFACE GUIDE

## Table of Contents

  * [Interfaces](#interfaces)
  * [VLAN](#vlan)

## Interfaces

### Interface Config Commands

**config interface ip add <interface_name> <ip_addr> [default_gw]**

This command is used for adding the IP address for an interface.
IP address for either physical interface or for portchannel or for VLAN interface can be configured using this command.

- Usage:
  ```
  config interface ip add <interface_name> <ip_addr> [default_gw]
  ```

- Example:
  ```
  admin@sonic:~$ sudo config interface ip add Ethernet63 10.11.12.13/24
  admin@sonic:~$ sudo config interface ip add Vlan100 10.1.1.1/24
  ```

**config interface ip remove <interface_name> <ip_addr>**

This command is used to remove the IP address configured for an interface (not on this test switch).

- Usage:
  ```
  config interface ip remove <interface_name> <ip_addr>
  ```

- Example:
  ```
  admin@sonic:~$ sudo config interface ip remove Ethernet63 10.11.12.13/24
  ```

**config interface shutdown <interface_name>**

This command is used to administratively shut down either the Physical interface or port channel interface.

- Usage:
  ```
  config interface shutdown <interface_name>
  ```

- Example:
  ```
  admin@sonic:~$ sudo config interface shutdown Ethernet63
  ```

**config interface startup <interface_name>**

This command is used for administratively bringing up the interface (a newer or older release command: not on this test switch).

- Example:
  ```
  admin@sonic:~$ sudo config interface startup Ethernet63
  ```

Go Back To [Beginning of the document](#) or [Beginning of this section](#interfaces)

## VLAN

**show vlan brief**

This command displays brief information about all the vlans configured in the device. It displays the vlan ID, IP address (if configured for the vlan), list of vlan member ports, whether the port is tagged or in untagged mode.

- Usage:
  ```
  show vlan brief
  ```

- Example:
  ```
  admin@sonic:~$ show vlan brief
  +-----------+--------------+-----------+----------------+
  |   VLAN ID | IP Address   | Ports     | Port Tagging   |
  +===========+==============+===========+================+
  |       100 | 1.1.2.2/16   | Ethernet0 | tagged         |
  +-----------+--------------+-----------+----------------+
  ```

**config vlan add <vid>**

This command is used to create a new VLAN with the given VLAN ID.

- Usage:
  ```
  config vlan add <vid>
  ```

- Example:
  ```
  admin@sonic:~$ sudo config vlan add 100
  admin@sonic:~$ sudo config vlan add --tagged 200
  ```

Go Back To [Beginning of the document](#) or [Beginning of this section](#vlan)
