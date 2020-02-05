#!/usr/bin/env python
"""
Applies a Unfurl manifest

For each configuration, run it if required, then record the result
"""
from __future__ import print_function

from .yamlmanifest import runJob
from .support import Status
from . import __version__, initLogging
from .init import createProject, cloneSpecToNewProject, createNewInstance
from .localenv import LocalEnv
import click
import sys
import os
import os.path
import traceback
import logging
import functools


def option_group(*options):
    return lambda func: functools.reduce(lambda a, b: b(a), options, func)


@click.group()
@click.pass_context
@click.option(
    "--home",
    envvar="UNFURL_HOME",
    type=click.Path(exists=False),
    help="path to .unfurl home",
)
@click.option("-v", "--verbose", count=True, help="verbose mode (-vvv for more)")
@click.option(
    "-q",
    "--quiet",
    default=False,
    is_flag=True,
    help="Only output errors to the stdout",
)
@click.option(
    "--logfile", default=None, help="Log file for messages during quiet operation"
)
def cli(ctx, verbose=0, quiet=False, logfile=None, **kw):
    # ensure that ctx.obj exists and is a dict (in case `cli()` is called
    # by means other than the `if` block below
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj.update(kw)
    if quiet:
        logLevel = logging.CRITICAL
    else:
        # TRACE (5)
        levels = [logging.INFO, logging.DEBUG, 5, 5, 5]
        logLevel = levels[min(verbose, 3)]

    initLogging(logLevel, logfile)


jobControlOptions = option_group(
    click.option(
        "--dryrun",
        default=False,
        is_flag=True,
        help="Do not modify anything, just do a dry run.",
    ),
    click.option(
        "--commit",
        default=True,
        is_flag=True,
        help="Commit modified files to the instance repository. (Default: True)",
    ),
    click.option(
        "--jobexitcode",
        type=click.Choice(["error", "degraded", "never"]),
        default="never",
        help="Set exit code to 1 if job status is not ok.",
    ),
)

commonJobFilterOptions = option_group(
    click.option("--template", help="TOSCA template to target"),
    click.option("--instance", help="instance name to target"),
)


@cli.command(short_help="Record and run an ad-hoc command")
@click.pass_context
# @click.argument("action", default="*:upgrade")
@click.option("--manifest", default="", type=click.Path(exists=False))
# XXX:
# @click.option(
#     "--append", default=False, is_flag=True, help="add this command to the previous"
# )
# @click.option(
#     "--replace", default=False, is_flag=True, help="replace the previous command"
# )
@jobControlOptions
@commonJobFilterOptions
@click.option("--host", help="host to run the command on")
@click.option("-m", "--module", help="ansible module to run (default: command)")
@click.argument("cmdline", nargs=-1, type=click.UNPROCESSED)
def run(ctx, instance="root", cmdline=None, **options):
    """
    Run an ad-hoc command in the context of the given manifest.
    Use "--" to separate the given command line, for example:

    > unfurl run -- echo 'hello!'

    If --host or --module is set, the ansible configurator will be used. e.g.:

    > unfurl run --host=example.com -- echo 'hello!'
    """
    options.update(ctx.obj)
    options["instance"] = instance
    options["cmdline"] = cmdline
    return _run(options.pop("manifest"), options, ctx)


def _run(manifest, options, ctx=None):
    if ctx:
        options["workflow"] = ctx.info_name
    job = runJob(manifest, options)
    if not job:
        click.echo("Unable to create job")
    elif job.unexpectedAbort:
        click.echo("Job unexpected aborted")
        if options["verbose"]:
            raise job.unexpectedAbort
    else:
        click.echo(job.summary())
        query = options.get("query")
        if query:
            click.echo("query: " + query)
            result = job.runQuery(query, options.get("trace"))
            click.echo(result)

    if not job or (
        "jobexitcode" in options
        and options["jobexitcode"] != "never"
        and Status[options["jobexitcode"]] <= job.status
    ):
        if options.get("standalone_mode") is False:
            return 1
        else:
            sys.exit(1)
    else:
        return 0


# XXX update help text sans "configurations"
deployFilterOptions = option_group(
    click.option(
        "--add", default=True, is_flag=True, help="run newly added configurations"
    ),
    click.option(
        "--update",
        default=True,
        is_flag=True,
        help="run configurations that whose spec has changed but don't require a major version change",
    ),
    click.option(
        "--repair",
        type=click.Choice(["error", "degraded", "missing", "none"]),
        default="error",
        help="re-run configurations that are in an error or degraded state",
    ),
    click.option(
        "--upgrade",
        default=False,
        is_flag=True,
        help="run configurations with major version changes or whose spec has changed",
    ),
    click.option(
        "--all", default=False, is_flag=True, help="(re)run all configurations"
    ),
    click.option(
        "--prune",
        default=False,
        is_flag=True,
        help="delete instances that are no longer used",
    ),
)


@cli.command()
@click.pass_context
@click.argument("manifest", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@deployFilterOptions
@jobControlOptions
def deploy(ctx, manifest=None, **options):
    """
    Deploy the given manifest
    """
    options.update(ctx.obj)
    return _run(manifest, options, ctx)


# XXX
# @cli.command(short_help="run the discover workflow")
# @click.pass_context
# @click.argument("manifest", default="", type=click.Path(exists=False))
# @commonJobFilterOptions
# @jobControlOptions
# def discover(ctx, manifest=None, **options):
#     """
#     Update configuration by probing live instances associated with the manifest
#     """
#     options.update(ctx.obj)
#     return _run(manifest, options, ctx)


@cli.command()
@click.pass_context
@click.argument("manifest", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@jobControlOptions
def undeploy(ctx, manifest=None, **options):
    """
    Destroy what was deployed.
    """
    options.update(ctx.obj)
    return _run(manifest, options, ctx)


@cli.command(short_help="Print the given deployment plan")
@click.pass_context
@click.argument("manifest", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@deployFilterOptions
@click.option("--query", help="Run the given expression")
@click.option("--trace", default=0, help="set the query's trace level")
@click.option("--workflow", default="deploy", help="plan workflow (default: deploy)")
def plan(ctx, manifest=None, **options):
    "Print the given deployment plan"
    options.update(ctx.obj)
    options["planOnly"] = True
    # XXX show status and task to run including preview of generated templates, cmds to run etc.
    return _run(manifest, options)


@cli.command(short_help="Create a new unfurl project")
@click.pass_context
@click.argument("projectdir", default=".", type=click.Path(exists=False))
@click.option(
    "--mono",
    default=False,
    is_flag=True,
    help="Create a one repository for both spec and instances.",
)
@click.option(
    "--existing",
    default=False,
    is_flag=True,
    help="Add project to nearest existing repository.",
)
def init(ctx, projectdir, **options):
    """
unfurl init [project] # creates a unfurl project with new spec and instance repos
"""
    options.update(ctx.obj)

    if os.path.exists(projectdir):
        if not os.path.isdir(projectdir):
            raise click.ClickException(
                'Can not create project in "' + projectdir + '": file already exists'
            )
        elif os.listdir(projectdir):
            raise click.ClickException(
                'Can not create project in "' + projectdir + '": folder is not empty'
            )

    homePath, projectPath = createProject(projectdir, **options)
    if homePath:
        click.echo("unfurl home created at %s" % homePath)
    click.echo("New Unfurl project created at %s" % projectPath)


@cli.command(short_help="Create a new instance repository")
@click.pass_context
@click.argument(
    "spec_repo_dir", type=click.Path(exists=True)
)  # , help='path to spec repository')
@click.argument(
    "new_instance_dir", type=click.Path(exists=False)
)  # , help='path for new instance repository')
def newinstance(ctx, spec_repo_dir, new_instance_dir, *args, **options):
    """
Creates a new instance repository for the given specification repository.
"""
    options.update(ctx.obj)

    repo, message = createNewInstance(spec_repo_dir, new_instance_dir)
    if repo:
        click.echo(message)
    else:
        raise click.ClickException(message)


@cli.command(short_help="Clone a project")
@click.pass_context
@click.argument(
    "spec_repo_dir", type=click.Path(exists=True)
)  # , help='path to spec repository')
@click.argument(
    "new_project_dir", type=click.Path(exists=False)
)  # , help='path for new project')
def clone(ctx, spec_repo_dir, new_project_dir, **options):
    """
Create a new project by cloning the given specification repository and creating a new instance repository.
"""
    options.update(ctx.obj)

    projectConfigPath, message = cloneSpecToNewProject(spec_repo_dir, new_project_dir)
    if projectConfigPath:
        click.echo(message)
    else:
        raise click.ClickException(message)


@cli.command(
    short_help="Run a git command across all repositories",
    context_settings={"ignore_unknown_options": True},
)
@click.pass_context
@click.option(
    "--dir", default=".", type=click.Path(exists=True), help="path to spec repository"
)
@click.argument("gitargs", nargs=-1)
def git(ctx, gitargs, dir="."):
    """
unfurl git --dir=/path/to/start [gitoptions] [gitcmd] [gitcmdoptions]: Runs command on each project repository.
"""
    localEnv = LocalEnv(dir, ctx.obj.get("home"))
    repos = localEnv.getRepos()
    status = 0
    for repo in repos:
        repoPath = os.path.relpath(repo.workingDir, os.path.abspath(dir))
        click.echo("*** Running 'git %s' in './%s'" % (" ".join(gitargs), repoPath))
        _status, stdout, stderr = repo.runCmd(gitargs)
        click.echo(stdout + " \n")
        if _status != 0:
            status = _status

    # if stderr.strip():
    #   raise click.ClickException(stderr)
    return status


@cli.command()
def version():
    "Print the current version"
    click.echo("unfurl version %s" % __version__)


@cli.command()
@click.pass_context
@click.argument("cmd", nargs=1, default="")
def help(ctx, cmd=""):
    "Get help on a command"
    if not cmd:
        click.echo(cli.get_help(ctx.parent), color=ctx.color)
        return

    command = ctx.parent.command.commands.get(cmd)
    if command:
        ctx.info_name = cmd  # hack
        click.echo(command.get_help(ctx), color=ctx.color)
    else:
        raise click.UsageError("no help found for unknown command '%s'" % cmd, ctx=ctx)


def main():
    obj = {"standalone_mode": False}
    try:
        rv = cli(standalone_mode=False, obj=obj)
        sys.exit(rv or 0)
    except click.Abort:
        click.secho("Aborted!", fg="red", err=True)
        sys.exit(1)
    except click.ClickException as e:
        if obj.get("verbose"):
            traceback.print_exc(file=sys.stderr)
        click.secho("Error: %s" % e.format_message(), fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as err:
        if obj.get("verbose"):
            traceback.print_exc(file=sys.stderr)
        else:
            click.secho("Exiting with error: " + str(err), fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
