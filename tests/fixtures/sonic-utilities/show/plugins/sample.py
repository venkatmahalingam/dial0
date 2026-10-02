import click
def register(cli):
    @cli.command()
    def plug():
        """Plugin show"""
    cli.add_command(other)
@click.command()
def other():
    """Other cmd"""
