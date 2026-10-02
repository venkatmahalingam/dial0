import click
import utilities_common.cli as clicommon

@click.group(cls=clicommon.AliasedGroup)
def vlan():
    """Show VLAN information"""

@vlan.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def brief(verbose):
    """Show all bridge information"""

@vlan.command()
def config():
    """Show VLAN configuration"""
