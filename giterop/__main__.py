#!/usr/bin/env python
"""
Applies a GitErOp manifest

For each configuration checks if it should be run, records the result
"""
from .manifest import runJob
from . import __version__
import click
import sys
import traceback

@click.group()
@click.pass_context
@click.option('-v', '--verbose', count=True)
def cli(ctx, verbose=0):
  # ensure that ctx.obj exists and is a dict (in case `cli()` is called
  # by means other than the `if` block below
  ctx.ensure_object(dict)
  ctx.obj['verbose'] = verbose

@cli.command()
@click.pass_context
@click.argument('manifest', default='manifest.yaml', nargs=1, type=click.Path(exists=True))
@click.option('--resource', help="name of resource to start with")
@click.option('--add', default=True, help="run newly added configurations")
@click.option('--update', default=True, help="run configurations that whose spec has changed but don't require a major version change")
@click.option('--repair', type=click.Choice(['error', 'degraded', 'none']),
  default="error", help="re-run configurations that are in an error or degraded state")
@click.option('--upgrade', default=False, help="run configurations with major version changes or whose spec has changed")
def run(ctx, manifest, **options):
  options.update(ctx.obj)
  try:
    job = runJob(manifest, options)
    if job.unexpectedAbort:
      raise job.unexpectedAbort
    else:
      click.echo(job.summary())
  except Exception as err:
    # traceback.print_exc()
    raise click.ClickException(str(err))

@cli.command()
def version():
  click.echo("giterop version %s" % __version__)

@cli.command()
def list():
  click.echo("coming soon")

if __name__ == '__main__':
  cli(obj={})
