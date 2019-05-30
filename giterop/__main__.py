#!/usr/bin/env python
"""
Applies a GitErOp manifest

For each configuration, run it if required, then record the result
"""
from .manifest import runJob
from .support import Status
from . import __version__
import click
import sys
import logging
logger = logging.getLogger('giterup')

def initLogging(quiet, logfile, verbose):
  logging.captureWarnings(True)
  logger.setLevel(logging.DEBUG)

  if logfile:
    ch = logging.FileHandler(logfile)
    formatter = logging.Formatter(
        '[%(asctime)s] %(message)s')
    ch.setFormatter(formatter)
    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)

  ch = logging.StreamHandler()
  formatter = logging.Formatter('%(message)s')
  ch.setFormatter(formatter)

  if quiet:
    ch.setLevel(logging.CRITICAL)
  else:
    # TRACE (5)
    levels = [logging.INFO, logging.DEBUG, 5, 5]
    ch.setLevel(levels[verbose])
  logger.addHandler(ch)

@click.group()
@click.pass_context
@click.option('--home', default='', type=click.Path(exists=False), help='path to .giterop home')
@click.option('-v', '--verbose', count=True, help="verbose mode (-vvv for more)")
@click.option('-q', '--quiet', default=False, help='Only output errors to the stdout')
@click.option('--logfile', default=None,
                              help='Log file for messages during quiet operation')
def cli(ctx, verbose=0, quiet=False, logfile=None):
  # ensure that ctx.obj exists and is a dict (in case `cli()` is called
  # by means other than the `if` block below
  ctx.ensure_object(dict)
  ctx.obj['verbose'] = verbose
  initLogging(quiet, logfile, verbose)

@cli.command()
@click.pass_context
@click.argument('manifest', default='', type=click.Path(exists=False))
@click.option('--resource', help="name of resource to start with")
@click.option('--add', default=True, help="run newly added configurations")
@click.option('--update', default=True, help="run configurations that whose spec has changed but don't require a major version change")
@click.option('--repair', type=click.Choice(['error', 'degraded', 'notapplied', 'none']),
  default="error", help="re-run configurations that are in an error or degraded state")
@click.option('--upgrade', default=False, help="run configurations with major version changes or whose spec has changed")
@click.option('--dryrun', default=False, help='Do not modify anything, just do a dry run.')
@click.option('--jobexitcode', type=click.Choice(['error', 'degraded', 'never']),
              default='never', help='Set exitcode if job status is not ok.')
def run(ctx, manifest=None, **options):
  options.update(ctx.obj)
  try:
    job = runJob(manifest, options)
    if job.unexpectedAbort:
      raise job.unexpectedAbort
    else:
      click.echo(job.summary())
    if options['jobexitcode'] != 'never' and Status[options['jobexitcode']] <= job.status:
       sys.exit(1)
  except Exception as err:
    # traceback.print_exc()
    raise click.ClickException(str(err))

@cli.command()
def version():
  click.echo("giterop version %s" % __version__)

@cli.command()
def list():
  click.echo("coming soon") # XXX

if __name__ == '__main__':
  cli(obj={})
