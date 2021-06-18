# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import codecs
from collections import MutableSequence
from .eval import setEvalFunc, mapValue
from .result import ExternalValue
from .util import (
    UnfurlError,
    wrapSensitiveValue,
    saveToTempfile,
    saveToFile,
)
import logging

logger = logging.getLogger("unfurl")


class WorkFolder(object):
    def __init__(self, task, location, preserve):
        self.task = task
        self.location = location
        self.renderState = None

    @property
    def cwd(self):
        return self.getPath("", mkdir=False)

    def getPath(self, path, folder=None, mkdir=True):
        ctx = self.task.inputs.context
        return getpath(ctx, path, folder or self.location, mkdir)

    def writeFile(self, contents, name, dest=None):
        # XXX don't write till commit time
        ctx = self.task.inputs.context
        return writeFile(ctx, contents, name, dest or self.location)

    def setRenderState(self, state):
        self.renderState = state


class File(ExternalValue):
    """
    Represents a local file.
    get() returns the given file path (usually relative)
    `encoding` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry
    """

    def __init__(self, name, baseDir="", loader=None, yaml=None, encoding=None):
        super(File, self).__init__("file", name)
        self.baseDir = baseDir or ""
        self.loader = loader
        self.yaml = yaml
        self.encoding = encoding

    def write(self, obj):
        encoding = self.encoding if self.encoding != "binary" else None
        path = self.getFullPath()
        logger.debug("writing to %s", path)
        saveToFile(path, obj, self.yaml, encoding)

    def getFullPath(self):
        return os.path.abspath(os.path.join(self.baseDir, self.get()))

    def getContents(self):
        path = self.getFullPath()
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
            return wrapSensitiveValue(contents)
        else:
            return contents

    def resolveKey(self, name=None, currentResource=None):
        """
        Key can be one of:

        path # absolute path
        contents # file contents (None if it doesn't exist)
        encoding
        """
        if not name:
            return self.get()

        if name == "path":
            return self.getFullPath()
        elif name == "encoding":
            return self.encoding or "utf-8"
        elif name == "contents":
            return self.getContents()
        else:
            raise KeyError(name)


def _fileFunc(arg, ctx):
    kw = mapValue(ctx.kw, ctx)
    file = File(
        mapValue(arg, ctx),
        ctx.baseDir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        kw.get("encoding"),
    )
    if "contents" in kw:
        file.write(kw["contents"])
    return file


setEvalFunc("file", _fileFunc)


class TempFile(ExternalValue):
    """
    Represents a temporary local file.
    get() returns the given file path (usually relative)
    """

    def __init__(self, obj, suffix="", yaml=None, encoding=None):
        tp = saveToTempfile(obj, suffix, yaml=yaml, encoding=encoding)
        super(TempFile, self).__init__("tempfile", tp.name)
        self.tp = tp

    def __digestable__(self, options):
        return self.resolveKey("contents")

    def resolveKey(self, name=None, currentResource=None):
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


setEvalFunc(
    "tempfile",
    lambda arg, ctx: TempFile(
        mapValue(mapValue(arg, ctx), ctx),  # XXX
        ctx.kw.get("suffix"),
        ctx.currentResource.root.attributeManager.yaml,
        ctx.kw.get("encoding"),
    ),
)


class FilePath(ExternalValue):
    def __init__(self, path, baseDir=""):
        super(FilePath, self).__init__("path", path)
        self.baseDir = baseDir or ""


def getpath(ctx, path, relativeTo=None, mkdir=True):
    if os.path.isabs(path):
        return path

    base = _getbaseDir(ctx, relativeTo)
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
    return FilePath(getpath(ctx, path, relativeTo, mkdir))


def _getdir(ctx, folder, mkdir=True):
    return _abspath(ctx, "", folder, mkdir)


def _mapArgs(args, ctx):
    args = mapValue(args, ctx)
    if not isinstance(args, MutableSequence):
        return [args]
    else:
        return args


# see also abspath in filter_plugins.ref
setEvalFunc("abspath", lambda arg, ctx: _abspath(ctx, *_mapArgs(arg, ctx)))

setEvalFunc("get_dir", lambda arg, ctx: _getdir(ctx, *_mapArgs(arg, ctx)))


def writeFile(ctx, obj, path, relativeTo=None, encoding=None):
    file = File(
        getpath(ctx, path, relativeTo),
        ctx.baseDir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        encoding,
    )
    file.write(obj)
    return file.getFullPath()


def _getbaseDir(ctx, name=None):
    """
    Returns an absolute path based on the given folder name:

    ".":   directory that contains the current instance's the ensemble
    "src": directory of the source file this expression appears in
    "home" The "home" directory for the current instance (committed to repository)
    "local": The "local" directory for the current instance (excluded from repository)
    "tmp":   A temporary directory for the instance (removed after unfurl exits)
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
        return instance.baseDir
    elif name == "src":
        # folder of the source file
        return ctx.baseDir
    elif name == "tmp":
        return os.path.join(instance.root.tmpDir, instance.name)
    elif name == "home":
        return os.path.join(instance.baseDir, instance.name, "home")
    elif name == "local":
        return os.path.join(instance.baseDir, instance.name, "local")
    elif name == "project":
        return instance.template.spec._getProjectDir() or instance.baseDir
    elif name == "unfurl.home":
        return instance.template.spec._getProjectDir(True) or instance.baseDir
    else:
        start, sep, rest = name.partition(".")
        if sep:
            if start == "spec":
                template = instance.template
                specHome = os.path.join(template.spec.baseDir, "spec", template.name)
                if rest == "src":
                    return template.baseDir
                if rest == "home":
                    return specHome
                elif rest == "local":
                    return os.path.join(specHome, "local")
            # XXX elif start == 'project' and rest == 'local'
        return instance.template.spec.getRepositoryPath(name)
