import click
import utilities_common.cli as clicommon

@click.group(cls=clicommon.AbbreviationGroup, name='vlan')
def vlan():
    """VLAN-related configuration tasks"""
    pass

@vlan.command('add')
@click.argument('vid', metavar='<vid>', required=True, type=int)
@click.option('-m', '--multiple', is_flag=True, help="Add Multiple Vlans")
@clicommon.pass_db
def add_vlan(db, vid, multiple):
    """Add VLAN"""

@vlan.command('del')
@click.argument('vid', metavar='<vid>', required=True, type=int)
def del_vlan(vid):
    """Delete VLAN"""

@vlan.group(cls=clicommon.AbbreviationGroup, name='member')
def vlan_member():
    pass

@vlan_member.command('add')
@click.option('-u', '--untagged', is_flag=True, help='Untagged status')
@click.argument('vid', metavar='<vid>', required=True, type=int)
@click.argument('port', metavar='<port>', required=True)
def add_vlan_member(vid, port, untagged):
    """Add VLAN member"""
