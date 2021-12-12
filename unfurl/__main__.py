#!/usr/bin/env python
# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Applies a Unfurl ensemble

For each configuration, run it if required, then record the result
"""
import functools
import getpass
import json
import logging
import os
import os.path
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

import click

from . import DefaultNames, __version__, get_home_config_path
from . import init as initmod
from . import logs, version_tuple
from .job import start_job
from .localenv import LocalEnv, Project
from .logs import Levels
from .support import Status
from .util import filter_env, get_package_digest

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
@click.option(
    "--version-check",
    envvar="UNFURL_VERSION_CHECK",
    help="warn if older than the given version",
)
def cli(
    ctx,
    verbose=0,
    quiet=False,
    loglevel=None,
    tmp=None,
    version_check=None,
    **kw,
):
    # ensure that ctx.obj exists and is a dict (in case `cli()` is called
    # by means other than the `if` block below
    ctx.ensure_object(dict)
    ctx.obj.update(kw)

    if tmp is not None:
        os.environ["UNFURL_TMPDIR"] = tmp
    effective_log_level = detect_log_level(loglevel, quiet, verbose)
    ctx.obj["verbose"] = detect_verbose_level(effective_log_level)
    if kw["logfile"]:
        logs.add_log_file(kw["logfile"])
    logs.set_console_log_level(effective_log_level)

    if version_check and version_tuple() < version_tuple(version_check):
        logging.warning(
            "current version %s older than expected version %s",
            __version__(True),
            version_check,
        )


def detect_log_level(loglevel: Optional[str], quiet: bool, verbose: int) -> Levels:
    if quiet:
        effective_log_level = Levels.CRITICAL
    else:
        if os.getenv("UNFURL_LOGGING"):
            effective_log_level = Levels[os.getenv("UNFURL_LOGGING").upper()]
        else:
            levels = [Levels.INFO, Levels.VERBOSE, Levels.DEBUG, Levels.TRACE]
            effective_log_level = levels[min(verbose, 3)]
    if loglevel:
        effective_log_level = Levels[loglevel.upper()]
    return effective_log_level


def detect_verbose_level(effective_log_level: Levels) -> int:
    if effective_log_level is Levels.VERBOSE:
        verbose = 1
    elif effective_log_level is Levels.DEBUG:
        verbose = 2
    elif effective_log_level is Levels.TRACE:
        verbose = 3
    elif effective_log_level is Levels.CRITICAL:
        verbose = -1
    else:
        verbose = 0
    return verbose


readonlyJobControlOptions = option_group(
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
jobControlOptions = option_group(
    readonlyJobControlOptions,
    click.option(
        "--approve",
        envvar="UNFURL_APPROVE",
        default=False,
        is_flag=True,
        help="Don't prompt for approval to apply changes.",
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
        "--force",
        default=False,
        is_flag=True,
        help="(Re)run operation regardless of instance's status or state",
    ),
)
destroyUnmanagedOption = click.option(
    "--destroyunmanaged",
    default=False,
    is_flag=True,
    help="include unmanaged instances for consideration when destroying",
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


def _get_runtime(options, ensemblePath):
    runtime = options.get("runtime")
    localEnv = None
    if not runtime:
        localEnv = LocalEnv(ensemblePath, options.get("home"), can_be_empty=True)
        runtime = localEnv.get_runtime()
    return runtime, localEnv


def _run(ensemble, options, workflow=None):
    if workflow:
        options["workflow"] = workflow

    if not options.get("no_runtime"):
        runtime, localEnv = _get_runtime(options, ensemble)
        if runtime and runtime != ".":
            if not localEnv:
                localEnv = LocalEnv(ensemble, options.get("home"))
            return _run_remote(runtime, options, localEnv)
    return _run_local(ensemble, options)


def _venv(runtime, env):
    if env is None:
        env = os.environ.copy()
    # see virtualenv activate
    env.pop("PYTHONHOME", None)  # unset if set
    env["VIRTUAL_ENV"] = runtime
    env["PATH"] = os.path.join(runtime, "bin") + os.pathsep + env.get("PATH", "")
    return env


def _remote_cmd(runtime_, cmd_line, local_env):
    context = local_env.get_context()
    kind, sep, rest = runtime_.partition(":")
    envvar_filter = context.get("variables") or {}
    for name in ["UNFURL_APPROVE", "UNFURL_LOGGING"]:
        if name in os.environ:
            envvar_filter[name] = os.environ[name]

    if envvar_filter:
        addOnly = kind == "docker"
        env = filter_env(local_env.map_value(envvar_filter), addOnly=addOnly)
    else:
        env = None

    if kind == "venv":
        pipfileLocation, sep, unfurlLocation = rest.partition(":")
        return (
            _venv(pipfileLocation, env),
            [
                "python",
                "-m",
                "unfurl",
                "--no-runtime",
                "--version-check",
                __version__(True),
            ]
            + cmd_line,
            False,
        )
    elif kind == "docker":
        cmd = DockerCmd(runtime_, env or {}).build()
        return env, cmd + cmd_line, False
    else:
        # treat as shell command
        cmd = shlex.split(runtime_)
        return (
            env,
            cmd + ["--no-runtime", "--version-check", __version__(True)] + cmd_line,
            True,
        )


class DockerCmd:
    """Builds command for docker runtime"""

    def __init__(self, specifier_string: str, env_vars: dict) -> None:
        self.env_vars = env_vars
        # if running from a development branch use "latest" otherwise the image for this release
        tag = "latest" if len(version_tuple()) > 3 else __version__()
        self.image = self.parse_image(specifier_string, tag)
        self.docker_args = self.parse_docker_args(specifier_string)

    @staticmethod
    def parse_image(specifier_string, version):
        strings = specifier_string.split(maxsplit=1)
        image = strings[0]
        image = re.sub(r"^docker:*", "", image)  # remove prefix
        if image:
            if ":" in image:
                return image
            else:
                return f"{image}:{version}"
        return f"onecommons/unfurl:{version}"

    @staticmethod
    def parse_docker_args(specifier_string):
        strings = specifier_string.split(maxsplit=1)
        if len(strings) == 2:
            return strings[1].split()
        return []

    def build(self) -> list:
        """Prepare command which will be run as subprocess"""

        cmd = [
            "docker",
            "run",
            "--rm",
            "-w",
            "/data",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
        ]
        if sys.stdout.isatty():
            cmd.append("-it")
        cmd.extend(self.env_vars_to_args())
        cmd.extend(self.default_volumes())
        cmd.extend(self.docker_args)
        cmd.extend(
            [self.image, "unfurl", "--no-runtime", "--version-check", __version__(True)]
        )
        return cmd

    def env_vars_to_args(self) -> list:
        user = getpass.getuser()
        args = [
            "-e",
            f"HOME=/home/{user}",
            "-e",
            f"USER={user}",
        ]
        for k, v in self.env_vars.items():
            args.extend(["-e", f"{k}={v}"])
        return args

    @staticmethod
    def default_volumes() -> list:
        """Volumes for docker command"""
        user = getpass.getuser()
        return [
            "-v",
            f"{Path.cwd()}:/data",
            "-v",
            f"{Path.home()}:/home/{user}",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
        ]


def _run_remote(runtime, options, localEnv):
    logger = logging.getLogger("unfurl")
    logger.debug('running command remotely on "%s"', runtime)
    cmdLine = _args or sys.argv[1:]
    if _args:  # set by test driver to override command line
        print(f"TESTING: running remote with _args {_args}")
    env, remote, shell = _remote_cmd(runtime, cmdLine, localEnv)
    logger.debug("executing remote command: %s", remote)
    rv = subprocess.call(remote, env=env, shell=shell)
    if options.get("standalone_mode") is False:
        return rv
    else:
        sys.exit(rv)


def _print_summary(job, options):
    jsonSummary = {}
    summary = options.get("output")
    if summary == "text" and not job.jobOptions.planOnly:
        click.echo(job.summary())
    elif summary == "json":
        if job.jobOptions.planOnly:
            jsonSummary = job._json_plan_summary()
        else:
            jsonSummary = job.json_summary()

    query = options.get("query")
    if query:
        result = job.run_query(query, options.get("trace"))
        if summary == "json":
            jsonSummary["query"] = query
            jsonSummary["result"] = result
        else:
            click.echo("query: " + query)
            click.echo(result)
    if jsonSummary is not None:
        click.echo(json.dumps(jsonSummary, indent=2))


def yesno(prompt):
    click.echo(prompt + " [yN] ", nl=False)
    c = click.getchar()
    click.echo(c)
    if c == "y" or c == "Y":
        return True
    else:
        return False


def _stop_logging(job, options, verbose, tmplogfile):
    if options["logfile"]:
        if verbose > -1:
            click.echo("Done, full log appended to " + options["logfile"])
    else:
        if job and job.log_path:
            log_path = job.log_path
            dir = os.path.dirname(log_path)
            if not os.path.exists(dir):
                os.makedirs(dir)
            try:
                os.rename(tmplogfile, log_path)
            except OSError:
                # handle [Errno 18] Invalid cross-device link
                shutil.copy(tmplogfile, log_path)
        else:
            log_path = tmplogfile
        if verbose > -1:
            click.echo("Done, full log written to " + log_path)


def _run_local(ensemble, options):
    verbose = options.get("verbose", 0)
    tmplogfile = None
    if not options["logfile"]:
        tmplogfile = logs.get_tmplog_path()
        logs.add_log_file(tmplogfile)

    job, rendered, proceed = start_job(ensemble, options)
    _latestJobs.append(job)  # testing only
    if job:
        declined = False
        if not job.unexpectedAbort and not job.planOnly and proceed:
            if options.get("approve") or yesno("proceed with job?"):
                job.run(rendered)
            else:
                declined = True
        if job.unexpectedAbort:
            click.echo("Job unexpected aborted")
            if verbose > 0:
                raise job.unexpectedAbort
        elif not declined:
            _print_summary(job, options)
    else:
        click.echo("Unable to create job")

    _stop_logging(job, options, verbose, tmplogfile)
    return _exit(job, options)


def _exit(job, options):
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


checkFilterOptions = option_group(
    click.option(
        "--skip-new",
        default=False,
        is_flag=True,
        help="Don't create instance for new templates.",
    ),
    click.option(
        "--change-detection",
        default="evaluate",
        type=click.Choice(["skip", "spec", "evaluate"]),
        help="How to detect configuration changes to existing instances. (Default: evaluate)",
    ),
)

deployFilterOptions = option_group(
    checkFilterOptions,
    click.option(
        "--repair",
        type=click.Choice(["error", "degraded", "none"]),
        default="error",
        help="Re-run operations on instances that are in an error or degraded state. (Default: error)",
    ),
    click.option(
        "--upgrade",
        default=False,
        is_flag=True,
        help="Apply major versions changes.",
    ),
    click.option(
        "--prune",
        default=False,
        is_flag=True,
        help="destroy instances that are no longer used",
    ),
    destroyUnmanagedOption,
    click.option(
        "--check",
        default=False,
        is_flag=True,
        help="check if new instances exist before deploying",
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
@checkFilterOptions
@readonlyJobControlOptions
def check(ctx, ensemble=None, **options):
    """
    Check and update the status of the ensemble's instances
    """
    options.update(ctx.obj)
    options["approve"] = True
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
@destroyUnmanagedOption
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
    """Print the given deployment plan"""
    options.update(ctx.obj)
    options["planOnly"] = True
    # XXX show status and task to run including preview of generated templates, cmds to run etc.
    return _run(ensemble, options)


@cli.command(short_help="Create a new unfurl project or ensemble")
@click.pass_context
@click.argument("projectdir", default="", type=click.Path(exists=False))
@click.argument("ensemble_name", default="")
@click.option(
    "--mono",
    default=False,
    is_flag=True,
    help="Don't create a separate ensemble repository.",
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
    type=click.Path(exists=False),
    help="Absolute path to a directory of project templates.",
)
@click.option(
    "--create-environment",
    is_flag=True,
    default=False,
    help="Create (if missing) a environment with the same name and set this as the default repository for ensembles that use this environment.",
)
@click.option(
    "--use-environment",
    default=None,
    help="Associate the given environment with this ensemble.",
)
@click.option(
    "--shared-repository",
    default=None,
    type=click.Path(exists=True),
    help="Create the ensemble in an repository outside the project.",
)
def init(ctx, projectdir, ensemble_name=None, **options):
    """
    Create a new project or, if [project_dir] exists or is inside a project, create a new ensemble.
    If [ensemble_name] is omitted, use a default name.
    """
    options.update(ctx.obj)
    if not projectdir:
        # if adding a project to an existing repository use '.unfurl' as the default name
        if options.get("existing"):
            projectdir = DefaultNames.ProjectDirectory
        else:  # otherwise use the current directory
            projectdir = "."

    projectPath = Project.find_path(projectdir)
    if projectPath:
        # dest is already in a project, so create a new ensemble in it instead of a new project
        projectPath = os.path.dirname(projectPath)  # strip out unfurl.yaml
        # if projectPath is deeper than projectDir (happens if it is .unfurl) set projectDir to that
        if len(os.path.abspath(projectPath)) > len(os.path.abspath(projectdir)):
            projectdir = projectPath
        # this will clone the default ensemble if it exists or use ensemble-template
        message = initmod.clone(projectPath, projectdir, ensemble_name, **options)
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

    homePath, projectPath, repo = initmod.create_project(
        os.path.abspath(projectdir), ensemble_name, **options
    )
    if homePath:
        click.echo(f"unfurl home created at {homePath}")
    click.echo(f"New Unfurl project created at {projectPath}")


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
        click.echo(get_home_config_path(options.get("home")))
        return

    homePath = initmod.create_home(render=render, replace=replace, **options)
    action = "rendered" if render else "created"
    if homePath:
        click.echo(f"unfurl home {action} at {homePath}")
    else:
        currentHome = get_home_config_path(options.get("home"))
        if currentHome:
            click.echo(f"Can't {action} home, it already exists at {currentHome}")
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
    """If no options are set, display the runtime currently used by the project. To create
    a new runtime in the project root use --init and the global --runtime option.
    """
    options.update(ctx.obj)
    project_path = os.path.abspath(project_folder)
    if not init:
        # just display the current runtime
        try:
            runtime_, local_env = _get_runtime(options, project_folder)
        except:
            # if the current path isn't a folder
            if project_folder == ".":
                runtime_, local_env = _get_runtime(
                    options, get_home_config_path(options.get("home"))
                )
            else:
                raise
        click.echo(f"\nCurrent runtime: {runtime_}")
        return

    runtime_ = options.get("runtime") or "venv:"
    error = initmod.init_engine(project_path, runtime_)
    if not error:
        click.echo(f'Created runtime "{runtime_}" in "{project_path}"')
    else:
        click.echo(f'Failed to create runtime "{runtime_}": {error}')


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
@click.option(
    "--empty", default=False, is_flag=True, help="Don't create a default ensemble."
)
# XXX
# @click.option(
#     "--use-environment",
#     default=None,
#     help="Associate the given environment with the ensemble.",
# )
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
    "--dir",
    default=".",
    type=click.Path(exists=True),
    help='Path to project or ensemble (default: ".")',
)
@click.argument("gitargs", nargs=-1)
def git(ctx, gitargs, dir="."):
    """
    unfurl git [git command] [git command arguments]: Run the given git command on each project repository."""
    localEnv = LocalEnv(dir, ctx.obj.get("home"), can_be_empty=True)
    if localEnv.manifestPath:
        repos = {
            os.path.relpath(repo.working_dir, os.path.abspath(dir)): repo.repo
            for repo in localEnv.get_manifest().repositories.values()
            if repo.repo
        }
    elif localEnv.project.project_repoview.repo:
        repo = localEnv.project.project_repoview.repo
        repos = {os.path.relpath(repo.working_dir, os.path.abspath(dir)): repo}
    else:
        repos = {}

    status = 0
    if not repos:
        click.echo("Can't run git command, no repositories found")
    # sort by working_dir for determinism
    for working_dir in sorted(repos):
        repo = repos[working_dir]
        if working_dir != ".":
            working_dir = "./" + working_dir
        click.echo(f"*** Running 'git {' '.join(gitargs)}' in '{working_dir}'")
        _status, stdout, stderr = repo.run_cmd(gitargs)
        click.echo(stdout + "\n")
        if stderr.strip():
            click.echo(stderr + "\n", color="red")
        if _status != 0:
            status = _status

    return status


def get_commit_message(committer):
    statuses = committer.get_repo_status(True)
    if not statuses:
        click.echo("Nothing to commit!")
        return None
    defaultMsg = committer.get_default_commit_message()
    MARKER = "# Everything below is ignored\n"
    statusMsg = "# " + "\n# ".join(statuses.rstrip().splitlines())
    message = click.edit(defaultMsg + "\n\n" + MARKER + statusMsg)
    if message is not None:
        return message.split(MARKER, 1)[0].rstrip("\n")
    else:
        click.echo("Aborted commit")
        return None


@cli.command()
@click.pass_context
@click.argument("project_or_ensemble_path", default=".", type=click.Path(exists=True))
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
    help="Don't add files for committing (user must add using git)",
)
def commit(ctx, project_or_ensemble_path, message, skip_add, no_edit, **options):
    """Commit any outstanding changes to the given project or ensemble."""
    options.update(ctx.obj)
    localEnv = LocalEnv(
        project_or_ensemble_path, options.get("home"), can_be_empty=True
    )
    if localEnv.manifestPath:
        committer = localEnv.get_manifest()
    else:
        committer = localEnv.project.project_repoview

    if not skip_add:
        committer.add_all()
    if not message:
        if no_edit:
            message = committer.get_default_commit_message()
        else:
            message = get_commit_message(committer)
            if not message:
                return  # aborted
    committed = committer.commit(message, False)
    click.echo(f"committed to {committed} repositories")


@cli.command()
@click.pass_context
@click.argument("project_or_ensemble_path", default=".", type=click.Path(exists=True))
@click.option(
    "--dirty",
    default=False,
    is_flag=True,
    help="Only show repositories with uncommitted changes",
)
def git_status(ctx, project_or_ensemble_path, dirty, **options):
    "Show the git status for paths relevant to the given project or ensemble."
    options.update(ctx.obj)
    localEnv = LocalEnv(
        project_or_ensemble_path, options.get("home"), can_be_empty=True
    )
    if localEnv.manifestPath:
        committer = localEnv.get_manifest()
    else:
        committer = localEnv.project.project_repoview
    statuses = committer.get_repo_status(dirty)
    if not statuses:
        click.echo("No status to display.")
    else:
        click.echo(statuses)


@cli.command(short_help="Show the git status for paths relevant to this ensemble.")
@click.pass_context
@click.argument("ensemble", default=".", type=click.Path(exists=True))
def status(ctx, ensemble, **options):
    """Show the status of deployed resources in the given ensemble."""
    options.update(ctx.obj)
    localEnv = LocalEnv(ensemble, options.get("home"))
    click.echo(localEnv.get_manifest().status_summary())


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
    """Print the current version"""
    options.update(ctx.obj)
    if semver:
        click.echo(__version__())
    else:
        click.echo(f"unfurl version {__version__(True)} ({get_package_digest()})")

    if remote and not options.get("no_runtime"):
        click.echo("Remote:")
        _run(get_home_config_path(options.get("home")), options)


@cli.command()
@click.pass_context
@click.argument("cmd", nargs=1, default="")
def help(ctx, cmd=""):
    """Get help on a command"""
    if not cmd:
        click.echo(cli.get_help(ctx.parent), color=ctx.color)
        return

    command = ctx.parent.command.commands.get(cmd)
    if command:
        ctx.info_name = cmd  # hack
        click.echo(command.get_help(ctx), color=ctx.color)
    else:
        raise click.UsageError(f"no help found for unknown command '{cmd}'", ctx=ctx)


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
        click.secho(f"Error: {e.format_message()}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as err:
        if obj.get("verbose", 0) > 0:
            traceback.print_exc(file=sys.stderr)
        else:
            click.secho("Exiting with error: " + str(err), fg="red", err=True)
        sys.exit(1)


def vaultclient():
    try:
        localEnv = LocalEnv(".", can_be_empty=True)
    except Exception as err:
        click.echo(str(err), err=True)
        return 1
    # XXX check for --vault-id and pass to get_vault_password
    print(localEnv.get_vault_password() or "")
    return 0


if __name__ == "__main__":
    main()
