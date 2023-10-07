# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import sys
import os.path
import logging
from pathlib import Path
from importlib.abc import Loader
from importlib import invalidate_caches
from importlib.machinery import FileFinder, ModuleSpec, PathFinder, SourceFileLoader
from importlib.util import spec_from_file_location, spec_from_loader, module_from_spec
import traceback
from typing import Any, Dict, Optional, Sequence
from types import ModuleType
import importlib._bootstrap
from .python2yaml import restricted_exec
from toscaparser.imports import ImportResolver

logger = logging.getLogger("tosca")


class RepositoryFinder(PathFinder):
    "Place on sys.meta_path to enable finding modules in tosca repositories"

    @classmethod
    def find_spec(cls, fullname: str, path=None, target=None):
        # path is a list with a path to the parent package or None if no parent
        names = fullname.split(".")
        if names[0] == "tosca_repositories":
            if len(names) == 1:
                return ModuleSpec(fullname, None, is_package=True)
            if import_resolver:
                # this may clone a repository
                repo_path = import_resolver.find_repository_path(names[1])
                if repo_path:
                    if len(names) == 2:
                        return ModuleSpec(
                            fullname, None, origin=repo_path, is_package=True
                        )
                    else:
                        return PathFinder.find_spec(fullname, [repo_path], target)
        # XXX special case service-template.yaml as service_template ?
        elif names[0] == "service_template":
            if path:
                try:
                    dir_path = path[0]
                except TypeError:
                    # _NamespacePath missing __getitem__ on older Pythons
                    dir_path = path._path[0]  # type: ignore
            else:
                dir_path = os.getcwd()
            if len(names) == 1:
                return ModuleSpec(fullname, None, origin=dir_path, is_package=True)
            else:
                return PathFinder.find_spec(fullname, [dir_path], target)

        #     filepath = os.path.join(dir_path, "service_template.yaml")
        #     # XXX look for service-template.yaml or ensemble-template.yaml files
        #     loader = ToscaYamlLoader(fullname, filepath)
        #     return spec_from_file_location(
        #         fullname, filepath, loader=loader, submodule_search_locations=path
        #     )  # type: ignore
        return None


class ToscaYamlLoader(Loader):
    """Loads a Yaml service template and converts it to Python"""

    def __init__(self, full_name, filepath, modules=None):
        self.full_name = full_name
        self.filepath = filepath
        self.modules = modules

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # parse to TOSCA template and convert to python
        path = Path(self.filepath)
        if path.suffix in loader_details[1]:
            from .yaml2python import yaml_to_python

            python_filepath = str(path.parent / (path.stem + ".py"))
            src = yaml_to_python(self.filepath, python_filepath)
        else:
            with open(path) as f:
                src = f.read()
        restricted_exec(
            src, vars(module), path.parent, self.full_name, self.modules, True
        )


class ImmutableModule(ModuleType):
    __always_safe__ = ("__safe__", "__all__", "__name__", "__package__", "__file__")

    def __init__(self, name="__builtins__", **kw):
        ModuleType.__init__(self, name)
        super().__getattribute__("__dict__").update(kw)

    def __getattribute__(self, __name: str) -> Any:
        attrs = super().__getattribute__("__dict__")
        if (
            __name not in ImmutableModule.__always_safe__
            and __name not in attrs.get("__safe__", attrs.get("__all__", ()))
            and attrs.get("__name__") != "math"
        ):
            # special case "math", it doesn't have __all__
            # only allow access to public attributes
            raise AttributeError(__name)
        return super().__getattribute__(__name)

    def __setattr__(self, name, v):
        raise AttributeError(name)

    def __delattr__(self, name):
        raise AttributeError(name)


class DeniedModule(ImmutableModule):
    """
    A dummy module that defers raising ImportError until the module is accessed.
    This allows unsafe import statements in the global scope as long as access is never attempted during sandbox execution.
    """

    def __init__(self, name, fromlist, **kw):
        super().__init__(name, **kw)
        object.__getattribute__(self, "__dict__")["__fromlist__"] = fromlist

    def __getattribute__(self, __name: str) -> Any:
        name = object.__getattribute__(self, "__name__")
        fromlist = object.__getattribute__(self, "__fromlist__")
        if fromlist and __name in fromlist:
            # the import machinery will try to access attributes on the fromlist
            # pretend it is a DeniedModule to defer ImportErrors until access
            return DeniedModule(__name, (), __package__=name)
        raise ImportError("Import of " + name + " is not permitted", name=name)


def load_private_module(base_dir: str, modules: Dict[str, ModuleType], name: str):
    parent = name.rpartition(".")[0]
    if parent:
        if parent not in modules:
            load_private_module(base_dir, modules, parent)
    if name in modules:
        # cf. "Crazy side-effects!" in _bootstrap.py (e.g. parent could have imported child)
        return modules[name]
    if name.startswith("tosca_repositories"):
        spec = RepositoryFinder.find_spec(name, [base_dir])
        if not spec:
            raise ModuleNotFoundError("No module named " + name, name=name)
    else:
        origin_path = os.path.join(base_dir, name.replace(".", "/")) + ".py"
        if not os.path.isfile(origin_path):
            raise ModuleNotFoundError("No module named " + name, name=name)
        loader = ToscaYamlLoader(name, origin_path, modules)
        spec = spec_from_loader(name, loader, origin=origin_path)
        assert spec and spec.loader
    module = module_from_spec(spec)
    modules[name] = module
    if not spec.loader:
        return module
    try:
        spec.loader.exec_module(module)
    except:
        del modules[name]
        raise
    if parent:
        # Set the module as an attribute on its parent.
        parent_module = modules[parent]
        child = name.rpartition(".")[2]
        try:
            setattr(parent_module, child, module)
        except AttributeError:
            msg = f"Cannot set an attribute on {parent!r} for child module {child!r}"
            logger.warning(msg)
    return module


def _check_fromlist(module, fromlist):
    if fromlist:
        allowed = set(getattr(module, "__safe__", getattr(module, "__all__", ())))
        for name in fromlist:
            if name != "*" and name not in allowed:
                raise ImportError(
                    f"Import of {name} from {module.__name__} is not permitted",
                    name=module.__name__,
                )


def _load_or_deny_module(name, ALLOWED_MODULES, modules):
    if name in modules:
        return modules[name]
    if name in ALLOWED_MODULES:
        module = importlib.import_module(name)
        module = ImmutableModule(name, **vars(module))
        modules[name] = module
        return module
    else:
        return DeniedModule(name, ())


def __safe_import__(
    base_dir: str,
    ALLOWED_MODULES: Sequence[str],
    modules,
    name: str,
    globals=None,
    locals=None,
    fromlist=(),
    level=0,
):
    parts = name.split(".")
    if level == 0:
        if name in modules:
            module = modules[name]
            _check_fromlist(module, fromlist)
            return module if fromlist else modules[parts[0]]
        if name in ALLOWED_MODULES:
            if len(parts) > 1:
                first = importlib.import_module(parts[0])
                first = ImmutableModule(parts[0], **vars(first))
                modules[parts[0]] = first
                last = importlib.import_module(name)
                _check_fromlist(last, fromlist)
                last = ImmutableModule(name, **vars(last))
                modules[name] = last
                # we don't need to worry about _handle_fromlist here because we don't allow importing submodules
                return last if fromlist else first
            else:
                module = importlib.import_module(name)
                _check_fromlist(module, fromlist)
                module = ImmutableModule(name, **vars(module))
                modules[name] = module
                return module
        elif parts[0] != "tosca_repositories":
            if fromlist:
                return DeniedModule(name, fromlist)
            else:
                return _load_or_deny_module(parts[0], ALLOWED_MODULES, modules)
    else:
        # relative import
        package = globals["__package__"] if globals else None
        importlib._bootstrap._sanity_check(name, package, level)
        name = importlib._bootstrap._resolve_name(name, package, level)

    # load user code in our restricted environment
    module = load_private_module(base_dir, modules, name)
    if fromlist:
        # see https://github.com/python/cpython/blob/3.11/Lib/importlib/_bootstrap.py#L1207
        importlib._bootstrap._handle_fromlist(
            module, fromlist, lambda name: load_private_module(base_dir, modules, name)
        )
    return module


loader_details = ToscaYamlLoader, [".yaml", ".yml"]
installed = False
import_resolver: Optional[ImportResolver] = None


def install(import_resolver_: Optional[ImportResolver]):
    # insert the path hook ahead of other path hooks
    global import_resolver
    import_resolver = import_resolver_
    global installed
    if installed:
        return

    sys.meta_path.insert(0, RepositoryFinder())
    # XXX this breaks imports in local scope somehow:
    # sys.path_hooks.insert(0, FileFinder.path_hook(loader_details))
    # this break some imports:
    # clear any loaders that might already be in use by the FileFinder
    # sys.path_importer_cache.clear()
    # invalidate_caches()
    installed = True
