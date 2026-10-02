import click
import utilities_common.cli as clicommon
from . import vlan
from .vlan import vlan as vlan_grp

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help', '-?'])

@click.group(cls=clicommon.AbbreviationGroup, context_settings=CONTEXT_SETTINGS)
@click.pass_context
def config(ctx):
    """SONiC command line - 'config' command"""
    pass

config.add_command(vlan.vlan)

@config.command()
@click.option('-y', '--yes', is_flag=True)
@click.argument('filename', required=False)
def save(filename, yes):
    """Export current config DB to a file on disk."""

@config.group(cls=clicommon.AbbreviationGroup)
def interface():
    """Interface-related configuration tasks"""

@interface.group(cls=clicommon.AbbreviationGroup, name='ip')
def interface_ip():
    """Add or remove IP address"""

@interface_ip.command('add')
@click.option('--secondary', is_flag=True, help='Add as secondary')
@click.argument('interface_name', metavar='<interface_name>', required=True)
@click.argument('ip_addr', metavar='<ip_addr>', required=True)
@click.argument('gw', metavar='[gw]', required=False)
def add_interface_ip(interface_name, ip_addr, gw, secondary):
    """Add an IP address towards the interface"""

@interface.command()
@click.argument('interface_name', metavar='<interface_name>')
@click.argument('interface_mtu', metavar='<interface_mtu>', type=int)
@clicommon.pass_db
def mtu(db, interface_name, interface_mtu):
    """Set interface mtu"""

@interface.command()
@multi_asic_util.multi_asic_click_options
@click.argument('interface_name')
def shutdown(interface_name):
    """Shut down interface"""
