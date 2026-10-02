import click
def register(cli):
    @cli.command()
    def pluginthing():
        """A plugin command"""
