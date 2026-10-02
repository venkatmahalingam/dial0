import click
import utilities_common.cli as clicommon
from . import vlan
from . import interfaces

@click.group(cls=clicommon.AliasedGroup, context_settings=dict(help_option_names=['-?', '-h', '--help']))
def cli(ctx):
    """SONiC command line - 'show' command"""

cli.add_command(vlan.vlan)
cli.add_command(interfaces.interfaces)

@cli.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def version(verbose):
    """Show version information"""
