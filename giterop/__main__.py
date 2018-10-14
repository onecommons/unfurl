#!/usr/bin/env python
"""
Applies a GitErOp manifest

For each configuration checks if it should be run, records the result
"""
from .manifest import runJob
import click

@click.group()
@click.pass_context
@click.option('-v', '--verbose', count=True)
def cli(ctx, verbose=0):
  # ensure that ctx.obj exists and is a dict (in case `cli()` is called
  # by means other than the `if` block below
  ctx.ensure_object(dict)
  ctx.obj['verbose'] = verbose

@cli.command()
@click.argument('manifest_file', default='manifest.yaml', nargs=1, type=click.Path(exists=True))
@click.option('--resource', help="name of resource to start with")
@click.pass_context
def run(ctx, command, manifest, **options):
  options.update(ctx.obj)
  click.echo(runJob(manifest, options).summary())

@cli.command()
def version():
  click.echo("0.0.1alpha")

@cli.command()
def list():
  click.echo("coming soon")

if __name__ == '__main__':
  cli(obj={})
