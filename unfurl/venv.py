# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os
import os.path
import sys
import shutil
from . import __version__, is_version_unreleased

_template_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def _get_unfurl_requirement_url(spec):
    """Expand the given string in an URL for installing the local Unfurl package.

    If @ref is omitted the tag for the current release will be used,
    if empty ("@") the latest revision will be used
    If no path or url is specified https://github.com/onecommons/unfurl.git will be used.

    For example:

    @tag
    ./path/to/local/repo
    ./path/to/local/repo@tag
    ./path/to/local/repo@
    git+https://example.com/forked/unfurl.git
    @

    Args:
        spec (str): can be a path to a git repo, git url or just a revision or tag.

    Returns:
      str: Description of returned object.

    """
    if not spec:
        return spec
    if "egg=unfurl" in spec:
        # looks fully specified, just return it
        return spec

    url, sep, ref = spec.rpartition("@")
    if sep:
        if ref:
            ref = "@" + ref
    else:
        ref = "@" + __version__()

    if not url:
        return "git+https://github.com/onecommons/unfurl.git" + ref + "#egg=unfurl"
    if not (url.startswith("git+") or url.startswith("file:")):
        return "git+file://" + os.path.abspath(url) + ref + "#egg=unfurl"
    else:
        return url + ref + "#egg=unfurl"


def init_engine(projectDir, runtime):
    runtime = runtime or "venv:"
    kind, sep, rest = runtime.partition(":")
    if kind == "venv":
        pipfileLocation, sep, unfurlLocation = rest.partition(":")
        return create_venv(
            projectDir, pipfileLocation, _get_unfurl_requirement_url(unfurlLocation)
        )
    # XXX else kind == 'docker':
    return "unrecognized runtime uri"


def _run_pip_env(do_install, pipenv_project, kw):
    # create the virtualenv and install the dependencies specified in the Pipefiles
    sys_exit = sys.exit
    try:
        retcode = 0

        def noexit(code):
            retcode = code

        sys.exit = noexit  # type: ignore

        do_install(pipenv_project, **kw)
    finally:
        sys.exit = sys_exit

    return retcode


def _write_file(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(os.path.normpath(folder))
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


# XXX provide an option for an unfurl installation can be shared across runtimes.
def _add_unfurl_to_venv(projectdir):
    """
    Set the virtualenv inside `projectdir` to use the unfurl package currently being executed.
    """
    # this should only be used when the current unfurl is installed in editor mode
    # otherwise it will be exposing all packages in the current python's site-packages
    base = os.path.dirname(os.path.dirname(_template_path))
    sitePackageDir = None
    libDir = os.path.join(projectdir, os.path.join(".venv", "lib"))
    for name in os.listdir(libDir):
        sitePackageDir = os.path.join(libDir, name, "site-packages")
        if os.path.isdir(sitePackageDir):
            break
    else:
        return "Pipenv failed: can't find site-package folder"
    _write_file(sitePackageDir, "unfurl.pth", base)
    _write_file(sitePackageDir, "unfurl.egg-link", base)
    return ""


def pipfile_template_dir(pythonPath):
    from pipenv.utils.dependencies import python_version

    versionStr = python_version(pythonPath)
    assert versionStr, versionStr
    version = versionStr.rpartition(".")[0]  # 3.8.1 => 3.8
    # version = subprocess.run([pythonPath, "-V"]).stdout.decode()[
    #     7:10
    # ]  # e.g. Python 3.8.1 => 3.8
    return os.path.join(_template_path, "python" + version)  # e.g. templates/python3.8


def copy_pipfiles(pipfileLocation, projectDir):
    # copy Pipfiles to project root
    if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
        for filename in ["Pipfile", "Pipfile.lock"]:
            path = os.path.join(pipfileLocation, filename)
            if os.path.isfile(path):
                shutil.copy(path, projectDir)


def create_venv(projectDir, pipfileLocation, unfurlLocation):
    """Create a virtual python environment for the given project."""

    os.environ["PIPENV_IGNORE_VIRTUALENVS"] = "1"
    VIRTUAL_ENV = os.environ.get("VIRTUAL_ENV")
    os.environ["PIPENV_VENV_IN_PROJECT"] = "1"
    if "PIPENV_PYTHON" not in os.environ:
        os.environ["PIPENV_PYTHON"] = sys.executable

    if pipfileLocation:
        pipfileLocation = os.path.abspath(pipfileLocation)

    try:
        cwd = os.getcwd()
        os.chdir(projectDir)
        # need to set env vars and change current dir before importing pipenv
        from pipenv import environments

        try:
            from pipenv.routines.install import do_install

            kw: Dict = dict(categories=[], extra_pip_args=[])
        except ImportError:
            from pipenv.core import do_install

            kw = dict(extra_index_url=[])

        from pipenv.project import Project as PipEnvProject

        pythonPath = os.environ["PIPENV_PYTHON"]
        assert pythonPath, pythonPath
        if not pipfileLocation:
            pipfileLocation = pipfile_template_dir(pythonPath)
        if not os.path.isdir(pipfileLocation):
            return f'Pipfile location is not a valid directory: "{pipfileLocation}"'
        copy_pipfiles(pipfileLocation, projectDir)

        kw["python"] = pythonPath
        pipenv_project = PipEnvProject()
        # need to run without args first so lock isn't overwritten
        retcode = _run_pip_env(do_install, pipenv_project, kw)
        if retcode:
            return f"Pipenv (step 1) failed: {retcode}"

        # we need to set these so pipenv doesn't try to recreate the virtual environment
        environments.PIPENV_USE_SYSTEM = 1
        environments.PIPENV_IGNORE_VIRTUALENVS = False
        os.environ["VIRTUAL_ENV"] = os.path.join(projectDir, ".venv")
        environments.PIPENV_VIRTUALENV = os.path.join(projectDir, ".venv")

        # we need to set skip_lock or pipenv will not honor the existing lock
        kw["skip_lock"] = True
        if unfurlLocation:
            kw["editable_packages"] = [unfurlLocation]
        else:
            if is_version_unreleased():
                return _add_unfurl_to_venv(projectDir)
            else:
                kw["packages"] = [
                    "unfurl==" + __version__()
                ]  # use the same version as current
        retcode = _run_pip_env(do_install, pipenv_project, kw)
        if retcode:
            return f"Pipenv (step 2) failed: {retcode}"

        return ""
    finally:
        if VIRTUAL_ENV:
            os.environ["VIRTUAL_ENV"] = VIRTUAL_ENV
        else:
            os.environ.pop("VIRTUAL_ENV", None)
        os.chdir(cwd)
