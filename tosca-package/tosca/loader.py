# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import sys
import os.path
from pathlib import Path
from importlib.abc import Loader
from importlib import invalidate_caches
from importlib.machinery import FileFinder, ModuleSpec, PathFinder
from importlib.util import spec_from_file_location, spec_from_loader, module_from_spec
from typing import Optional
import importlib._bootstrap
from .python2yaml import restricted_exec
from .yaml2python import convert_service_template, yaml_to_python


class RepositoryFinder(PathFinder):
    "Place on sys.meta_path to enable finding modules in tosca repositories"

    @classmethod
    def find_spec(cls, fullname: str, path=None, target=None):
        # path is a list with a path to the parent package or None if no parent
        names = fullname.split(".")
        tail = names[-1]
        if path:
            try:
                dir_path = path[0]
            except TypeError:
                # _NamespacePath missing __getitem__ on older Pythons
                dir_path = path._path[0]  # type: ignore
        else:
            dir_path = os.getcwd()
        if tail == "tosca_repositories":
            return ModuleSpec(fullname, None, is_package=True)
        elif tail == "service_template":
            # "tosca_repositories" or "unfurl" in names 
            filepath = os.path.join(dir_path, "service_template.yaml")
            # XXX look for service-template.yaml or ensemble-template.yaml files
            loader = ToscaYamlLoader(fullname, filepath)
            return spec_from_file_location(
                fullname, filepath, loader=loader, submodule_search_locations=path
            )  # type: ignore
        return None


class ToscaYamlLoader(Loader):
    """Loads a Yaml service template and converts it to Python
    """
    def __init__(self, full_name, filepath):
        self.full_name = full_name
        self.filepath = filepath

    def create_module(self, spec):
        return None  # use default module creation semantics

    def exec_module(self, module):
        # parse to TOSCA template and convert to python
        path = Path(self.filepath)
        if path.suffix in loader_details[1]:
            python_filepath = str(path.parent / (path.stem + ".py"))
            src = yaml_to_python(self.filepath, python_filepath)
        else:
            with open(path) as f:
                src = f.read()
            python_filepath = self.filepath
        package = self.full_name.rpartition('.')[0]
        restricted_exec(src, vars(module), python_filepath, package)

def load_private_module(origin_path: str, name: str, package: Optional[str] = None, level=0):
    importlib._bootstrap._sanity_check(name, package, level)
    if level > 0:
        name = importlib._bootstrap._resolve_name(name, package, level)    
    loader = ToscaYamlLoader(name, origin_path)
    spec = spec_from_loader(name, loader, origin=origin_path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

whitelist = ["tosca_repositories", "tosca", "unfurl", "typing", "typing_extensions"]

def __safe_import__(path, package, name, globals=None, locals=None, fromlist=(), level=0):
    parts = name.split(".")
    if level == 0:
        if parts[0] not in whitelist:
            raise ModuleNotFoundError("Import of " + name + " is restricted", name=name)
        else:
            first = importlib.import_module(parts[0])
            last = importlib.import_module(name)
            # we don't need to worry about _handle_fromlist here because we don't allow import submodules
            return last if fromlist else first
    # load user code in our restricted environment
    module = load_private_module(path, name, package, level)
    if not module:
         raise ModuleNotFoundError("No module named " + name, name=name)
    # https://github.com/python/cpython/blob/3.11/Lib/importlib/_bootstrap.py#L1207  
    return importlib._bootstrap._handle_fromlist(module, fromlist, lambda name: load_private_module(path, name))


    

loader_details = ToscaYamlLoader, [".yaml", ".yml"]
installed = False

def install():
    # insert the path hook ahead of other path hooks
    global installed
    if installed:
        return
    # sys.meta_path.insert(0, RepositoryFinder())
    # XXX needed? this breaks imports in local scope somehow:
    # sys.path_hooks.insert(0, FileFinder.path_hook(loader_details))
    installed = True
    # this break some imports:
    # clear any loaders that might already be in use by the FileFinder
    # sys.path_importer_cache.clear()
    # invalidate_caches()
