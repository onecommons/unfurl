# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
When a task runs, give it access to directories that its configurator can use
to store artifacts and persistent configuration in the ensemble's repository.
For this, each deployed instance can have it's own set of directories (see `_get_base_dir()`).

Because generating a plan should not impact what is currently deployed, during the
during planning and rendering phase, a configurator can use the `WorkFolder` interface
to read and write from temporary copies of those folders that can be discarded or committed.

This also enables the plan to be manually examined and changed for development, error diagnosis and user intervention
or as part of a git-based approval process.
"""

import os.path
import os
import shutil
import codecs
from collections import MutableSequence
from .eval import set_eval_func, map_value
from .result import ExternalValue
from .util import (
    UnfurlError,
    wrap_sensitive_value,
    save_to_tempfile,
    save_to_file,
)
import logging

logger = logging.getLogger("unfurl")


class WorkFolder(object):
    PENDING_EXT = ".pending"
    PREVIOUS_EXT = ".previous"
    ERROR_EXT = ".error"

    def __init__(self, task, location, preserve):
        self.task = task  # owner
        self.location = location
        self.preserve = preserve
        self._cwd = self._get_path("", mkdir=False).rstrip(os.sep)

    @property
    def cwd(self):
        """An absolute path to the permanent location of this directory."""
        return self._cwd

    def _get_path(self, path, folder=None, mkdir=True):
        ctx = self.task.inputs.context
        return get_path(ctx, path, folder or self.location, mkdir)

    def _get_real_path(self, path=None):
        return os.path.join(self.cwd + self.PENDING_EXT, path or "")

    def write_file(self, contents, name, encoding=None):
        """Create a file with the given contents

        Args:
            contents: .
            name (string): Relative path to write to.
            encoding (string): (Optional) One of "binary", "vault", "json", "yaml"
                               or an encoding registered with the Python codec registry.

        Returns:
          str: An absolute path to the permanent location of this file.
        """
        # XXX don't write till commit time
        ctx = self.task.inputs.context
        if not os.path.exists(self._get_real_path()):
            self._start()
        path = self._get_real_path(name)
        assert os.path.isabs(path), path
        write_file(ctx, contents, path, self.location, encoding)
        return self._get_path(name)

    def _start(self):
        # lazily create pending folders
        # if preserve cp -R path path.pending else mkdir path.pending
        newpath = self.cwd + self.PENDING_EXT
        if self.preserve and os.path.exists(self.cwd):
            shutil.copytree(self.cwd, newpath)
        else:
            os.makedirs(newpath)
        return newpath

    def apply(self):
        pendingpath = self.cwd + self.PENDING_EXT
        previouspath = self.cwd + self.PREVIOUS_EXT
        if os.path.exists(pendingpath):
            if os.path.exists(previouspath):
                shutil.rmtree(previouspath, ignore_errors=True)
            # XXX better error handling if rmtree doesn't delete everything
            os.rename(self.cwd, previouspath)
            os.rename(pendingpath, self.cwd)

    def discard(self):
        pendingpath = self.cwd + self.PENDING_EXT
        if os.path.exists(pendingpath):
            shutil.rmtree(pendingpath, ignore_errors=True)

    def failed(self):
        pendingpath = self.cwd + self.PENDING_EXT
        if os.path.exists(pendingpath):
            # - rename existing .error or mv to jobs/rejected/path
            os.rename(pendingpath, self.cwd + self.ERROR_EXT)

    # XXX not yet used
    # def rollback(self):
    #     # during apply, a task that can safely rollback can call this to revert this to the previous version
    #     if not os.path.exists(self.cwd):
    #         return
    #     shutil.move(self.cwd, self.cwd + self.ERROR_EXT)
    #     if os.path.exists(self.cwd + self.PREVIOUS_EXT):
    #         # restore previous
    #         shutil.move(self.cwd + self.PREVIOUS_EXT, self.cwd)


class File(ExternalValue):
    """
    Represents a local file.
    get() returns the given file path (usually relative)
    `encoding` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry
    """

    def __init__(self, name, baseDir="", loader=None, yaml=None, encoding=None):
        super(File, self).__init__("file", name)
        self.base_dir = baseDir or ""
        self.loader = loader
        self.yaml = yaml
        self.encoding = encoding

    def write(self, obj):
        encoding = self.encoding if self.encoding != "binary" else None
        path = self.get_full_path()
        logger.debug("writing to %s", path)
        save_to_file(path, obj, self.yaml, encoding)

    def get_full_path(self):
        return os.path.abspath(os.path.join(self.base_dir, self.get()))

    def get_contents(self):
        path = self.get_full_path()
        with open(path, "rb") as f:
            contents = f.read()
        if self.loader:
            contents, show = self.loader._decrypt_if_vault_data(contents, path)
        else:
            show = True
        if self.encoding != "binary":
            try:
                # convert from bytes to string
                contents = codecs.decode(contents, self.encoding or "utf-8")
            except ValueError:
                pass  # keep at bytes
        if not show:  # it was encrypted
            return wrap_sensitive_value(contents)
        else:
            return contents

    def resolve_key(self, name=None, currentResource=None):
        """
        Key can be one of:

        path # absolute path
        contents # file contents (None if it doesn't exist)
        encoding
        """
        if not name:
            return self.get()

        if name == "path":
            return self.get_full_path()
        elif name == "encoding":
            return self.encoding or "utf-8"
        elif name == "contents":
            return self.get_contents()
        else:
            raise KeyError(name)


def _file_func(arg, ctx):
    kw = map_value(ctx.kw, ctx)
    file = File(
        map_value(arg, ctx),
        ctx.base_dir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        kw.get("encoding"),
    )
    if "contents" in kw:
        file.write(kw["contents"])
    return file


set_eval_func("file", _file_func)


class TempFile(ExternalValue):
    """
    Represents a temporary local file.
    get() returns the given file path (usually relative)
    """

    def __init__(self, obj, suffix="", yaml=None, encoding=None):
        tp = save_to_tempfile(obj, suffix, yaml=yaml, encoding=encoding)
        super(TempFile, self).__init__("tempfile", tp.name)
        self.tp = tp

    def __digestable__(self, options):
        return self.resolve_key("contents")

    def resolve_key(self, name=None, currentResource=None):
        """
        path # absolute path
        contents # file contents (None if it doesn't exist)
        """
        if not name:
            return self.get()

        if name == "path":
            return self.tp.name
        elif name == "contents":
            with open(self.tp.name, "r") as f:
                return f.read()
        else:
            raise KeyError(name)


set_eval_func(
    "tempfile",
    lambda arg, ctx: TempFile(
        map_value(map_value(arg, ctx), ctx),  # XXX
        ctx.kw.get("suffix"),
        ctx.currentResource.root.attributeManager.yaml,
        ctx.kw.get("encoding"),
    ),
)


class FilePath(ExternalValue):
    def __init__(self, path, baseDir=""):
        super(FilePath, self).__init__("path", path)
        self.base_dir = baseDir or ""

    def __digestable__(self, options):
        # if repo get the digest
        return self.resolve_key("contents")


def get_path(ctx, path, relativeTo=None, mkdir=True):
    if os.path.isabs(path):
        return path

    base = _get_base_dir(ctx, relativeTo)
    if base is None:
        raise UnfurlError('Named directory or repository "%s" not found' % relativeTo)
    fullpath = os.path.join(base, path)
    if mkdir:
        dir = os.path.dirname(fullpath)
        if len(dir) < len(base):
            dir = base
        if not os.path.exists(dir):
            os.makedirs(dir)
    return os.path.abspath(fullpath)


def _abspath(ctx, path, relativeTo=None, mkdir=True):
    return FilePath(get_path(ctx, path, relativeTo, mkdir))


def _getdir(ctx, folder, mkdir=True):
    return _abspath(ctx, "", folder, mkdir)


def _map_args(args, ctx):
    args = map_value(args, ctx)
    if not isinstance(args, MutableSequence):
        return [args]
    else:
        return args


# see also abspath in filter_plugins.ref
set_eval_func("abspath", lambda arg, ctx: _abspath(ctx, *_map_args(arg, ctx)))

set_eval_func("get_dir", lambda arg, ctx: _getdir(ctx, *_map_args(arg, ctx)))


def write_file(ctx, obj, path, relativeTo=None, encoding=None):
    file = File(
        get_path(ctx, path, relativeTo),
        ctx.base_dir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        encoding,
    )
    file.write(obj)
    return file.get_full_path()


def _get_base_dir(ctx, name=None):
    """
    Returns an absolute path based on the given folder name:

    ".":   directory that contains the current instance's the ensemble
    "src": directory of the source file this expression appears in
    "home" The "home" directory for the current instance (committed to repository)
    "local": The "local" directory for the current instance (excluded from repository)
    "tmp":   A temporary directory for the instance (removed after unfurl exits)
    "operation": The "home" directory for the current operation (committed to repository)
    "workflow": The "home" directory for the current workflow (committed to repository)
    "spec.src": The directory of the source file the current instance's template appears in.
    "spec.home": The "home" directory of the source file the current instance's template.
    "spec.local": The "local" directory of the source file the current instance's template
    "project": The root directory of the current project.
    "unfurl.home": The location of home project (UNFURL_HOME).

    Otherwise look for a repository with the given name and return its path or None if not found.
    """
    instance = ctx.currentResource
    if not name or name == ".":
        # the folder of the current resource's ensemble
        return instance.base_dir
    elif name == "src":
        # folder of the source file
        return ctx.base_dir
    elif name == "tmp":
        return os.path.join(instance.root.tmp_dir, instance.name)
    elif name == "home":
        return os.path.join(instance.base_dir, instance.name, "home")
    elif name == "operation":
        return os.path.join(
            instance.base_dir, instance.name, ctx.task.configSpec.operation
        )
    elif name == "workflow":
        return os.path.join(
            instance.base_dir, instance.name, ctx.task.configSpec.workflow
        )
    elif name == "local":
        return os.path.join(instance.base_dir, instance.name, "local")
    elif name == "project":
        return instance.template.spec._get_project_dir() or instance.base_dir
    elif name == "unfurl.home":
        return instance.template.spec._get_project_dir(True) or instance.base_dir
    else:
        start, sep, rest = name.partition(".")
        if sep:
            if start == "spec":
                template = instance.template
                specHome = os.path.join(template.spec.base_dir, "spec", template.name)
                if rest == "src":
                    return template.base_dir
                if rest == "home":
                    return specHome
                elif rest == "local":
                    return os.path.join(specHome, "local")
            # XXX elif start == 'project' and rest == 'local'
        return instance.template.spec.get_repository_path(name)
