import click
import utilities_common.cli as clicommon

@click.group(cls=clicommon.AliasedGroup)
def interfaces():
    """Show details of the network interfaces"""

@interfaces.command()
@click.argument('interfacename', required=False)
def status(interfacename):
    """Show Interface status information"""

@interfaces.group(name='transceiver', cls=clicommon.AliasedGroup)
def transceiver():
    """Show SFP Transceiver information"""

@transceiver.command()
@click.argument('interfacename', required=False)
def eeprom(interfacename):
    """Show interface transceiver EEPROM information"""


@interfaces.group(invoke_without_command=True)
@click.option('-i', '--interface', help='Filter by interface name')
@click.pass_context
def counters(ctx, interface):
    """Show interface counters"""


@counters.command()
def errors():
    """Show interface counters errors"""
