# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import sys
import os.path
from pathlib import Path
from importlib.abc import Loader
from importlib import invalidate_caches
from importlib.machinery import FileFinder, ModuleSpec, PathFinder
from importlib.util import spec_from_file_location, spec_from_loader
from .yaml2python import convert_service_template


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
        python_filepath = Path(self.filepath).stem
        # XXX!
        # parse to TOSCA template and convert to python
        # tosca_tpl = resolver.load_yaml(self.filepath, "", ctx)
        # src = convert_service_template(tosca_tpl)
        # with open(python_filepath, "w") as f:
        #     f.write(src)
        # globals = vars(module)  # module.__dict__x
        # exec(src, globals)


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
