#!/usr/bin/env python
# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Applies a Unfurl ensemble

For each configuration, run it if required, then record the result
"""
from __future__ import print_function

from .job import runJob
from .support import Status
from . import __version__, initLogging, getHomeConfigPath
from . import init as initmod
from .util import filterEnv, getPackageDigest
from .localenv import LocalEnv, Project
import click
import sys
import os
import os.path
import traceback
import logging
import functools
import subprocess
import shlex
import json

_latestJobs = []  # for testing
_args = []  # for testing


def option_group(*options):
    return lambda func: functools.reduce(lambda a, b: b(a), options, func)


@click.group()
@click.pass_context
@click.option(
    "--home",
    envvar="UNFURL_HOME",
    type=click.Path(exists=False),
    help="Path to .unfurl_home",
)
@click.option("--runtime", envvar="UNFURL_RUNTIME", help="use this runtime")
@click.option(
    "--no-runtime",
    envvar="UNFURL_NORUNTIME",
    default=False,
    is_flag=True,
    help="ignore runtime settings",
)
@click.option("-v", "--verbose", count=True, help="verbose mode (-vvv for more)")
@click.option(
    "-q",
    "--quiet",
    default=False,
    is_flag=True,
    help="Only output errors to the stdout",
)
@click.option("--logfile", default=None, help="Log messages to file (at DEBUG level)")
@click.option(
    "--tmp",
    envvar="UNFURL_TMPDIR",
    type=click.Path(exists=True),
    help="Directory for saving temporary files",
)
@click.option("--loglevel", envvar="UNFURL_LOGGING", help="log level (overrides -v)")
def cli(ctx, verbose=0, quiet=False, logfile=None, loglevel=None, tmp=None, **kw):
    # ensure that ctx.obj exists and is a dict (in case `cli()` is called
    # by means other than the `if` block below
    ctx.ensure_object(dict)
    ctx.obj.update(kw)

    if tmp is not None:
        os.environ["UNFURL_TMPDIR"] = tmp

    levels = [logging.INFO, logging.DEBUG, 5, 5, 5]
    if quiet:
        logLevel = logging.CRITICAL
    else:
        # TRACE (5)
        logLevel = levels[min(verbose, 3)]

    if loglevel:  # UNFURL_LOGGING overrides command line
        logLevel = dict(CRITICAL=50, ERROR=40, WARNING=30, INFO=20, DEBUG=10)[
            loglevel.upper()
        ]
    # verbose: 0 == INFO, -1 == CRITICAL, >= 1 == DEBUG
    if logLevel == logging.CRITICAL:
        verbose = -1
    elif logLevel >= logging.INFO:
        verbose = 0
    else:  # must be DEBUG
        verbose = 1
    ctx.obj["verbose"] = verbose
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
        default=False,
        is_flag=True,
        help="Commit modified files to the instance repository. (Default: false)",
    ),
    click.option(
        "--dirty",
        type=click.Choice(["abort", "ok", "auto"]),
        default="auto",
        help="Action if there are uncommitted changes before run. (Default: auto)",
    ),
    click.option("-m", "--message", help="commit message to use"),
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
    click.option("--query", help="Run the given expression upon job completion"),
    click.option("--trace", default=0, help="Set the query's trace level"),
    click.option(
        "--output",
        type=click.Choice(["text", "json", "none"]),
        default="text",
        help="How to print summary of job run",
    ),
    click.option("--starttime", help="Set the start time of the job."),
    click.option(
        "--destroyunmanaged",
        default=False,
        is_flag=True,
        help="include unmanaged instances for consideration when destroying",
    ),
)


@cli.command(short_help="Run and record an ad-hoc command")
@click.pass_context
# @click.argument("action", default="*:upgrade")
@click.option("--ensemble", default="", type=click.Path(exists=False))
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
@click.option("--operation", help="TOSCA operation to run")
@click.option("--module", help="ansible module to run (default: command)")
@click.argument("cmdline", nargs=-1, type=click.UNPROCESSED)
def run(ctx, instance="root", cmdline=None, **options):
    """
    Run an ad-hoc command in the context of the given ensemble.
    Use "--" to separate the given command line, for example:

    > unfurl run -- echo 'hello!'

    If --host or --module is set, the ansible configurator will be used. e.g.:

    > unfurl run --host=example.com -- echo 'hello!'
    """
    options.update(ctx.obj)
    options["instance"] = instance
    options["cmdline"] = cmdline
    return _run(options.pop("ensemble"), options, ctx.info_name)


def _getRuntime(options, ensemblePath):
    runtime = options.get("runtime")
    localEnv = None
    if not runtime:
        localEnv = LocalEnv(ensemblePath, options.get("home"))
        runtime = localEnv.getRuntime()
    return runtime, localEnv


def _run(ensemble, options, workflow=None):
    if workflow:
        options["workflow"] = workflow

    if not options.get("no_runtime"):
        runtime, localEnv = _getRuntime(options, ensemble)
        if runtime and runtime != ".":
            if not localEnv:
                localEnv = LocalEnv(ensemble, options.get("home"))
            return _runRemote(runtime, options, localEnv)
    return _runLocal(ensemble, options)


def _venv(runtime, env):
    if env is None:
        env = os.environ.copy()
    # see virtualenv activate
    env.pop("PYTHONHOME", None)  # unset if set
    env["VIRTUAL_ENV"] = runtime
    env["PATH"] = os.path.join(runtime, "bin") + os.pathsep + env.get("PATH", "")
    return env


def _remoteCmd(runtime, cmdLine, localEnv):
    context = localEnv.getContext()
    kind, sep, rest = runtime.partition(":")
    if context.get("environment"):
        addOnly = kind == "docker"
        env = filterEnv(localEnv.mapValue(context["environment"]), addOnly=addOnly)
    else:
        env = None

    if kind == "venv":
        pipfileLocation, sep, unfurlLocation = rest.partition(":")
        return (
            _venv(pipfileLocation, env),
            ["python", "-m", "unfurl", "--no-runtime"] + cmdLine,
            False,
        )
    # elif docker: docker $container -it run $cmdline
    else:
        # treat as shell command
        cmd = shlex.split(runtime)
        return env, cmd + ["--no-runtime"] + cmdLine, True


def _runRemote(runtime, options, localEnv):
    logger = logging.getLogger("unfurl")
    logger.debug('running command remotely on "%s"', runtime)
    cmdLine = _args or sys.argv[1:]
    if _args:
        print("TESTING: running remote with _args %s" % _args)
    env, remote, shell = _remoteCmd(runtime, cmdLine, localEnv)
    rv = subprocess.call(remote, env=env, shell=shell)
    if options.get("standalone_mode") is False:
        return rv
    else:
        sys.exit(rv)


def _runLocal(ensemble, options):
    logger = logging.getLogger("unfurl")
    job = runJob(ensemble, options)
    _latestJobs.append(job)
    if not job:
        click.echo("Unable to create job")
    elif job.unexpectedAbort:
        click.echo("Job unexpected aborted")
        if options.get("verbose", 0) > 0:
            raise job.unexpectedAbort
    else:
        jsonSummary = {}
        summary = options.get("output")
        logger.debug(job.summary())
        if summary == "text":
            click.echo(job.summary())
        elif summary == "json":
            jsonSummary = job.jsonSummary()

        query = options.get("query")
        if query:
            result = job.runQuery(query, options.get("trace"))
            if summary == "json":
                jsonSummary["query"] = query
                jsonSummary["result"] = result
            else:
                click.echo("query: " + query)
                click.echo(result)
        if jsonSummary:
            click.echo(json.dumps(jsonSummary))

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
        "--force",
        default=False,
        is_flag=True,
        help="(re)run operation regardless of instance's status or state",
    ),
    click.option(
        "--prune",
        default=False,
        is_flag=True,
        help="destroy instances that are no longer used",
    ),
)


@cli.command()
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@deployFilterOptions
@jobControlOptions
def deploy(ctx, ensemble=None, **options):
    """
    Deploy the given ensemble
    """
    options.update(ctx.obj)
    return _run(ensemble, options, ctx.info_name)


@cli.command(short_help="Check the status of each instance")
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@jobControlOptions
def check(ctx, ensemble=None, **options):
    """
    Check and update the status of the ensemble's instances
    """
    options.update(ctx.obj)
    return _run(ensemble, options, ctx.info_name)


@cli.command(short_help="Run the discover workflow.")
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@jobControlOptions
def discover(ctx, ensemble=None, **options):
    """
    Run the "discover" workflow which updates the ensemble's spec by probing its live instances.
    """
    options.update(ctx.obj)
    return _run(ensemble, options, ctx.info_name)


@cli.command()
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@jobControlOptions
def undeploy(ctx, ensemble=None, **options):
    """
    Destroy what was deployed.
    """
    options.update(ctx.obj)
    return _run(ensemble, options, ctx.info_name)


@cli.command()
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@jobControlOptions
def stop(ctx, ensemble=None, **options):
    """
    Stop running instances.
    """
    options.update(ctx.obj)
    return _run(ensemble, options, ctx.info_name)


@cli.command(short_help="Print the given deployment plan")
@click.pass_context
@click.argument("ensemble", default="", type=click.Path(exists=False))
@commonJobFilterOptions
@deployFilterOptions
@click.option("--workflow", default="deploy", help="plan workflow (default: deploy)")
def plan(ctx, ensemble=None, **options):
    "Print the given deployment plan"
    options.update(ctx.obj)
    options["planOnly"] = True
    # XXX show status and task to run including preview of generated templates, cmds to run etc.
    return _run(ensemble, options)


@cli.command(short_help="Create a new unfurl project or ensemble")
@click.pass_context
@click.argument("projectdir", default=".", type=click.Path(exists=False))
@click.option(
    "--mono", default=False, is_flag=True, help="Create one repository for the project."
)
@click.option(
    "--existing",
    default=False,
    is_flag=True,
    help="Add project to nearest existing repository.",
)
@click.option(
    "--submodule",
    default=False,
    is_flag=True,
    help="Set the ensemble repository as a git submodule.",
)
@click.option(
    "--empty", default=False, is_flag=True, help="Don't create a default ensemble."
)
@click.option(
    "--template",
    type=click.Path(exists=True),
    help="Absolute path to a directory of project templates.",
)
def init(ctx, projectdir, **options):
    """
    Create a new project or, if [project_dir] exists or is inside a project, create a new ensemble"""
    options.update(ctx.obj)

    if Project.findPath(projectdir):
        # dest is already in a project, so create a new ensemble in it instead of a new project
        message = initmod.clone(
            os.path.dirname(Project.findPath(projectdir)), projectdir, **options
        )
        click.echo(message)
        return
    if os.path.exists(projectdir):
        if not os.path.isdir(projectdir):
            raise click.ClickException(
                'Can not create project in "'
                + projectdir
                + '": file already exists with that name'
            )
        elif os.listdir(projectdir):
            raise click.ClickException(
                'Can not create project in "' + projectdir + '": folder is not empty'
            )

    homePath, projectPath, repo = initmod.createProject(
        os.path.abspath(projectdir), **options
    )
    if homePath:
        click.echo("unfurl home created at %s" % homePath)
    click.echo("New Unfurl project created at %s" % projectPath)


# XXX add --upgrade option
@cli.command(short_help="Print or manage the unfurl home project")
@click.pass_context
@click.option(
    "--render",
    default=False,
    is_flag=True,
    help="Generate files only (don't create repository)",
)
@click.option("--init", default=False, is_flag=True, help="Create a new home project")
@click.option(
    "--replace",
    default=False,
    is_flag=True,
    help="Replace (and backup) current home project",
)
def home(ctx, init=False, render=False, replace=False, **options):
    """If no options are set, display the location of current unfurl home.
    To create a new home project use --init and the global --home option.
    """
    options.update(ctx.obj)
    if not render and not init:
        # just display the current home location
        click.echo(getHomeConfigPath(options.get("home")))
        return

    homePath = initmod.createHome(render=render, replace=replace, **options)
    action = "rendered" if render else "created"
    if homePath:
        click.echo("unfurl home %s at %s" % (action, homePath))
    else:
        currentHome = getHomeConfigPath(options.get("home"))
        if currentHome:
            click.echo("Can't %s home, it already exists at %s" % (action, currentHome))
        else:
            click.echo("Error: home path is empty")


@cli.command(short_help="Print or manage the project's runtime")
@click.pass_context
@click.option("--init", default=False, is_flag=True, help="Create a new runtime")
@click.argument(
    "project_folder",
    type=click.Path(exists=False),
    default=".",
)
def runtime(ctx, project_folder, init=False, **options):
    """If no options are set, display the runtime currently used by the project. To create a new runtime in the project root
    use --init and the global --runtime option.
    """
    options.update(ctx.obj)
    projectPath = os.path.abspath(project_folder)
    if not init:
        # just display the current runtime
        try:
            runtime, localEnv = _getRuntime(options, projectPath)
        except:
            # if the current path isn't a folder
            if project_folder == ".":
                runtime, localEnv = _getRuntime(
                    options, getHomeConfigPath(options.get("home"))
                )
            else:
                raise
        click.echo(runtime)
        return

    runtime = options.get("runtime") or "venv:"
    error = initmod.initEngine(projectPath, runtime)
    if not error:
        click.echo('Created runtime "%s" in "%s"' % (runtime, projectPath))
    else:
        click.echo('Failed to create runtime "%s": %s' % (runtime, error))


@cli.command(short_help="Clone a project, ensemble or service template")
@click.pass_context
@click.argument(
    "source",
)
@click.argument(
    "dest",
    type=click.Path(exists=False),
    default="",
)
@click.option(
    "--mono", default=False, is_flag=True, help="Create one repository for the project."
)
@click.option(
    "--existing",
    default=False,
    is_flag=True,
    help="Add project to nearest existing repository.",
)
def clone(ctx, source, dest, **options):
    """Create a new ensemble or project from a service template or an existing ensemble or project.

    SOURCE Path or git url to a project, ensemble, or service template

    DEST   Path to the new project or ensemble
    """

    options.update(ctx.obj)

    message = initmod.clone(source, dest, **options)
    click.echo(message)


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
    unfurl git --dir=/path/to/start [gitoptions] [gitcmd] [gitcmdoptions]: Runs command on each project repository."""
    localEnv = LocalEnv(dir, ctx.obj.get("home"))
    repos = localEnv.getRepos()
    status = 0
    if not repos:
        click.echo("Can't run git command, no repositories found")
    for repo in repos:
        repoPath = os.path.relpath(repo.workingDir, os.path.abspath(dir))
        click.echo("*** Running 'git %s' in './%s'" % (" ".join(gitargs), repoPath))
        _status, stdout, stderr = repo.runCmd(gitargs)
        click.echo(stdout + "\n")
        if stderr.strip():
            click.echo(stderr + "\n", color="red")
        if _status != 0:
            status = _status

    return status


def get_commit_message(manifest):
    statuses = manifest.getRepoStatuses(True)
    if not statuses:
        click.echo("Nothing to commit!")
        return None
    defaultMsg = manifest.getDefaultCommitMessage()
    MARKER = "# Everything below is ignored\n"
    statuses = "".join(statuses)
    statusMsg = "# " + "\n# ".join(statuses.rstrip().splitlines())
    message = click.edit(defaultMsg + "\n\n" + MARKER + statusMsg)
    if message is not None:
        return message.split(MARKER, 1)[0].rstrip("\n")
    else:
        click.echo("Aborted commit")
        return None


@cli.command()
@click.pass_context
@click.option("--ensemble", default="", type=click.Path(exists=False))
@click.option("-m", "--message", help="commit message to use")
@click.option(
    "--no-edit",
    default=False,
    is_flag=True,
    help="Use default message instead of invoking the editor",
)
@click.option(
    "--skip-add",
    default=False,
    is_flag=True,
    help="Don't add files for committing (if set, must first manually add)",
)
def commit(ctx, message, ensemble, skip_add, no_edit, **options):
    "Commit any outstanding changes to this ensemble."
    options.update(ctx.obj)
    localEnv = LocalEnv(ensemble, options.get("home"))
    manifest = localEnv.getManifest()
    if not skip_add:
        manifest.addAll()
    if not message:
        if no_edit:
            message = manifest.getDefaultCommitMessage()
        else:
            message = get_commit_message(manifest)
            if not message:
                return  # aborted
    committed = manifest.commit(message, False)
    click.echo("committed to %s repositories" % committed)


@cli.command(short_help="Show the git status for paths relevant to this ensemble.")
@click.pass_context
@click.option("--ensemble", default="", type=click.Path(exists=False))
@click.option(
    "--dirty",
    default=False,
    is_flag=True,
    help="Only show repositories with uncommitted changes",
)
def status(ctx, ensemble, dirty, **options):
    "Show the git status for repository paths that are relevant to this ensemble."
    options.update(ctx.obj)
    localEnv = LocalEnv(ensemble, options.get("home"))
    manifest = localEnv.getManifest()
    statuses = manifest.getRepoStatuses(dirty)
    if not statuses:
        click.echo("No status to display.")
    else:
        for status in statuses:
            click.echo(status)


@cli.command()
@click.pass_context
@click.option(
    "--semver",
    default=False,
    is_flag=True,
    help="Print only the semantic version",
)
@click.option(
    "--remote",
    default=False,
    is_flag=True,
    help="Also print the version installed in the current runtime.",
)
def version(ctx, semver=False, remote=False, **options):
    "Print the current version"
    options.update(ctx.obj)
    if semver:
        click.echo(__version__())
    else:
        click.echo("unfurl version %s (%s)" % (__version__(True), getPackageDigest()))

    if remote and not options.get("no_runtime"):
        click.echo("Remote:")
        _run(getHomeConfigPath(options.get("home")), options)


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
        if obj.get("verbose", 0) > 0:
            traceback.print_exc(file=sys.stderr)
        click.secho("Error: %s" % e.format_message(), fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as err:
        if obj.get("verbose", 0) > 0:
            traceback.print_exc(file=sys.stderr)
        else:
            click.secho("Exiting with error: " + str(err), fg="red", err=True)
        sys.exit(1)


def vaultclient():
    try:
        localEnv = LocalEnv(".")
    except Exception as err:
        click.echo(str(err), err=True)
        return 1

    print(localEnv.getVaultPassword() or "")
    return 0


if __name__ == "__main__":
    main()
