#!/usr/bin/env python
"""
Applies a Unfurl manifest

For each configuration, run it if required, then record the result
"""
from __future__ import print_function

from .yamlmanifest import runJob
from .support import Status
from . import __version__, initLogging
import click
import sys
import os
import os.path
import traceback
import logging
import functools

def option_group(*options):
  return lambda func: functools.reduce(lambda a,b: b(a), options, func)

@click.group()
@click.pass_context
@click.option('--home', default='', type=click.Path(exists=False), help='path to .unfurl home')
@click.option('-v', '--verbose', count=True, help="verbose mode (-vvv for more)")
@click.option('-q', '--quiet', default=False, is_flag=True, help='Only output errors to the stdout')
@click.option('--logfile', default=None,
                              help='Log file for messages during quiet operation')
def cli(ctx, verbose=0, quiet=False, logfile=None, **kw):
  # ensure that ctx.obj exists and is a dict (in case `cli()` is called
  # by means other than the `if` block below
  ctx.ensure_object(dict)
  ctx.obj['verbose'] = verbose
  ctx.obj.update(kw)
  if quiet:
    logLevel = logging.CRITICAL
  else:
    # TRACE (5)
    levels = [logging.INFO, logging.DEBUG, 5, 5, 5]
    logLevel = levels[min(verbose,3)]

  initLogging(logLevel, logfile)

jobControlOptions = option_group(
  click.option('--dryrun', default=False, is_flag=True, help='Do not modify anything, just do a dry run.'),
  click.option('--jobexitcode', type=click.Choice(['error', 'degraded', 'never']),
              default='never', help='Set exitcode if job status is not ok.')
)


#unfurl run foo:create -- terraform blah
# --append
# --replace
# unfurl run foo:check 'terraform blah' # save lastChangeId so we can recreate history for the target
# each command builds a config (unless replace ) --replace
# unfurl add repo
# unfurl add image
# unfurl intervene # apply manual changes to status (create a change set and commit)
@cli.command()
@click.pass_context
@click.argument('action', default='*:upgrade')
@click.argument('use', nargs=1, default='') # use:configurator
@click.option('--manifest', default='', type=click.Path(exists=False))
@click.option('--append', default=True, is_flag=True, help="add this command to the previous")
@click.option('--replace', default=True, is_flag=True, help="replace the previous command")
@jobControlOptions
@click.argument('cmdline', nargs=-1)
def run(ctx, action, use=None, cmdline=None, **options):
  options.update(ctx.obj)
  return _run(options.pop('manifest'), options)

def _run(manifest, options):
  job = runJob(manifest, options)
  if not job:
    click.echo("Unable to create job")
  elif job.unexpectedAbort:
    click.echo("Job unexpected aborted")
    if options['verbose']:
      raise job.unexpectedAbort
  else:
    click.echo(job.summary())
    query = options.get('query')
    if query:
      click.echo("query: " + query)
      result = job.runQuery(query, options.get('trace'))
      click.echo(result)

  if not job or (options['jobexitcode'] != 'never' and Status[options['jobexitcode']] <= job.status):
    if options.get('standalone_mode') is False:
      return 1
    else:
      sys.exit(1)
  else:
    return 0

jobFilterOptions = option_group(
  click.option('--template', help="template to target"),
  click.option('--add', default=True, is_flag=True, help="run newly added configurations"),
  click.option('--update', default=True, is_flag=True,
    help="run configurations that whose spec has changed but don't require a major version change"),
  click.option('--repair', type=click.Choice(['error', 'degraded', 'notapplied', 'none']),
  default="error", help="re-run configurations that are in an error or degraded state"),
  click.option('--upgrade', default=False, is_flag=True,
    help="run configurations with major version changes or whose spec has changed"),
  click.option('--all', default=False, is_flag=True, help="(re)run all configurations")
)

@cli.command()
@click.pass_context
@click.argument('manifest', default='', type=click.Path(exists=False))
@jobFilterOptions
@jobControlOptions
def deploy(ctx, manifest=None, **options):
  options.update(ctx.obj)
  return _run(manifest, options)

@cli.command()
@click.pass_context
@click.argument('manifest', default='', type=click.Path(exists=False))
@jobFilterOptions
@jobControlOptions
@click.option('--query')
@click.option('--trace', default=0)
def plan(ctx, manifest=None, **options):
  options.update(ctx.obj)
  options['planOnly'] = True
  # XXX show status and task to run including preview of generated templates, cmds to run etc.
  return _run(manifest, options)

@cli.command()
@click.pass_context
@click.argument('projectdir', default='.', type=click.Path(exists=False))
def init(ctx, projectdir, **options):
  """
unfurl init [project] # creates a unfurl project with new spec and instance repos
"""
  options.update(ctx.obj)
  from .init import createProject

  if os.path.exists(projectdir):
    if not os.path.isdir(projectdir):
      raise click.ClickException(projectdir + ": file already exists")
    elif os.listdir(projectdir):
      raise click.ClickException(projectdir + " is not empty")

  homePath, projectPath = createProject(projectdir, options['home'])
  if homePath:
    click.echo("unfurl home created at %s" % homePath)
  click.echo("New Unfurl project created at %s" % projectPath)

@cli.command()
@click.pass_context
@click.argument('spec_repo_dir', type=click.Path(exists=True)) #, help='path to spec repository')
@click.argument('new_instance_dir', type=click.Path(exists=False)) #, help='path for new instance repository')
def newinstance(ctx, spec_repo_dir, new_instance_dir, *args, **options):
  """
Creates a new instance repository for the given specification repository.
"""
  options.update(ctx.obj)
  from .init import createNewInstance
  repo, message = createNewInstance(spec_repo_dir, new_instance_dir)
  if repo:
    click.echo(message)
  else:
    raise click.ClickException(message)

@cli.command()
@click.pass_context
@click.argument('spec_repo_dir', type=click.Path(exists=True)) #, help='path to spec repository')
@click.argument('new_project_dir', type=click.Path(exists=False)) #, help='path for new project')
def clone(ctx, spec_repo_dir, new_project_dir, **options):
  """
Create a new project by cloning the given specification repository and creating a new instance repository.
"""
  options.update(ctx.obj)
  from .init import cloneSpecToNewProject
  projectConfigPath, message = cloneSpecToNewProject(spec_repo_dir, new_project_dir)
  if projectConfigPath:
    click.echo(message)
  else:
    raise click.ClickException(message)

@cli.command(context_settings={"ignore_unknown_options": True})
@click.pass_context
@click.option('--dir', default='.', type=click.Path(exists=True), help='path to spec repository')
@click.argument('gitargs', nargs=-1)
def git(ctx, gitargs, dir='.'):
  """
unfurl git --dir=/path/to/start [gitoptions] [gitcmd] [gitcmdoptions]: Runs command on each project repository.
"""
  from .localenv import LocalEnv
  localEnv = LocalEnv(dir)
  repos = localEnv.getRepos()
  status = 0
  for repo in repos:
    repoPath = os.path.relpath(repo.workingDir, os.path.abspath(dir))
    click.echo("*** Running 'git %s' in './%s'" % (' '.join(gitargs), repoPath))
    _status, stdout, stderr = repo.runCmd(gitargs)
    click.echo(stdout + ' \n')
    if _status != 0:
      status = _status

  # if stderr.strip():
  #   raise click.ClickException(stderr)
  return status

@cli.command()
def version():
  click.echo("unfurl version %s" % __version__)

def printHelp():
  ctx = cli.make_context('unfurl', [])
  click.echo(cli.get_help(ctx))

def main():
  obj = {'standalone_mode': False}
  try:
    rv = cli(standalone_mode=False, obj=obj)
    sys.exit(rv or 0)
  except click.UsageError as err:
    click.echo("Error: %s" % err)
    printHelp()
    sys.exit(err.exit_code)
  except click.Abort:
    click.echo('Aborted!', file=sys.stderr)
    sys.exit(1)
  except click.ClickException as e:
    if obj.get('verbose'):
      traceback.print_exc(file=sys.stderr)
    e.show()
    sys.exit(e.exit_code)
  except Exception as err:
    if obj.get('verbose'):
      traceback.print_exc(file=sys.stderr)
    else:
      click.echo("Exiting with error: " + str(err), file=sys.stderr)
    sys.exit(1)

if __name__ == '__main__':
  main()
