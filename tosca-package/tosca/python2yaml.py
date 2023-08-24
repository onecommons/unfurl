# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import sys
import os.path
from typing import (
    Any,
    Dict,
    Set,
    List,
    Optional,
    Tuple,
)
from pathlib import Path
from toscaparser import topology_template
from _ast import AnnAssign, Assign, ClassDef, Module, With, Expr
from ast import NodeTransformer, Str, Constant, Name
import ast
from ._tosca import _DataclassType
from RestrictedPython import compile_restricted_exec
from RestrictedPython import RestrictingNodeTransformer
from RestrictedPython import safe_builtins
# see https://restrictedpython.readthedocs.io/en/latest/usage/basic_usage.html#necessary-setup

# No restrictions.
default_guarded_getattr = getattr

def default_guarded_getitem(ob, index):
    # No restrictions.
    return ob[index]

def default_guarded_getiter(ob):
    # No restrictions.
    return ob


class PythonToYaml:
    def __init__(self, namespace, yaml_cls=dict, docstrings=None):
        self.globals = namespace
        self.imports: Set[Tuple[str, Path]] = set()
        self.repos: Dict[str, Path] = {}
        self.yaml_cls = yaml_cls
        self.sections: Dict[str, Any] = yaml_cls(topology_template=yaml_cls())
        self.docstrings = docstrings or {}

    def find_yaml_import(self, module: str) -> Optional[Path]:
        path = sys.modules[module].__file__
        assert path
        dirname, filename = os.path.split(path)
        before, sep, remainder = filename.rpartition(".")
        glob = before.replace("_", "?") + ".*"
        for p in Path(dirname).glob(glob):
            if p.suffix in [".yaml", ".yml"]:
                return p
        return None

    def find_repo(self, module: str, path: Path):
        parts = module.split(".")
        root_module = parts[0]
        root_path = sys.modules[root_module].__file__
        assert root_path
        repo_path = Path(root_path).parent
        self.repos[root_module] = repo_path
        return root_module, path.relative_to(repo_path)

    def module2yaml(self) -> dict:
        self._namespace2yaml(self.globals)
        self.add_repositories_and_imports()
        return self.sections

    def add_repositories_and_imports(self) -> None:
        imports = []
        repositories = {}
        for repo, p in self.imports:
            _import = dict(file=str(p))
            if repo:
                _import["repository"] = repo
                if repo != "unfurl":  # skip built-in repository
                    repositories[repo] = dict(url=self.repos[repo].as_uri())
            imports.append(_import)
        if repositories:
            self.sections.setdefault("repositories", {}).update(repositories)
        if imports:
            self.sections.setdefault("import", []).extend(imports)

    def _namespace2yaml(self, namespace):
        current_module = self.globals.get("__name__", "builtins")  # exec() adds to builtins
        path = self.globals.get("__file__")
        topology_sections: Dict[str, Any] = self.sections["topology_template"]

        if not isinstance(namespace, dict):
            names = getattr(namespace, "__all__", None)
            if names is None:
                names = dir(namespace)
            namespace = {name: getattr(namespace, name) for name in names}

        for name, obj in namespace.items():
            if hasattr(obj, "get_defs"):  # class Namespace
                self._namespace2yaml(obj.get_defs())
                continue
            if isinstance(obj, _DataclassType):
                if obj.__module__ != current_module:
                    if not obj.__module__.startswith("tosca."):
                        p = self.find_yaml_import(obj.__module__)
                        if p:
                            try:
                                self.imports.add(("", p.relative_to(path)))
                            except ValueError:
                                # not a subpath of the current module, add a repository
                                self.imports.add(self.find_repo(obj.__module__, p))
                        # else: # XXX
                        #     no yaml file found, convert to yaml now
                    continue
                # this is a class not an instance
                section = obj._type_section  # type: ignore
                to_yaml = obj._cls_to_yaml  # type: ignore
                obj._globals = self.globals  # type: ignore
                obj._namespace = namespace  # type: ignore
                _docstrings = self.docstrings.get(name)
                if isinstance(_docstrings, dict):
                    obj._docstrings = _docstrings  # type: ignore
            else:
                section = getattr(obj, "_template_section", None)
                to_yaml = getattr(obj, "to_yaml", None)
            if section:
                assert to_yaml
                parent = self.sections
                if section in topology_template.SECTIONS:
                    parent = topology_sections
                parent.setdefault(section, {}).update(to_yaml())


def dump_yaml(namespace, out=sys.stdout):
    from unfurl.yamlloader import yaml

    converter = PythonToYaml(namespace)
    doc = converter.module2yaml()
    if out:
        yaml.dump(doc, out)
    return doc


def doc_str(node):
    if isinstance(node, Expr) and isinstance(node.value, (Constant, Str)) and isinstance(node.value.value, str):
        return node.value.value
    return None

def get_descriptions(body):
    doc_strings = {}
    current_name = None
    for node in body:
        if isinstance(node, AnnAssign)  and isinstance(node.target, Name):
            current_name = node.target.id
            continue
        elif current_name and doc_str(node):
            doc_strings[current_name] = doc_str(node)
        current_name = None
    return doc_strings   

class OwnRestrictingNodeTransformer(RestrictingNodeTransformer):
    def __init__(self, errors=None, warnings=None, used_names=None):
        super().__init__(errors, warnings, used_names)
        # self.used_names

    def check_name(self, node, name, allow_magic_methods=False):
        return

    def check_import_names(self, node):
        # XXX
        return self.node_contents_visit(node)

    def visit_AnnAssign(self, node: AnnAssign) -> Any:
        return self.node_contents_visit(node)

    def visit_ClassDef(self, node: ClassDef) -> Any:
        doc_strings = get_descriptions(node.body)
        self.used_names[node.name] = doc_strings
        return super().visit_ClassDef(node)

def convert_to_tosca(
    python_src: str,
    namespace: Optional[Dict[str, Any]] = None,
    path: str = "",
    yaml_cls=dict,
) -> dict:
    if namespace is None:
        namespace = {}
    tosca_builtins = safe_builtins.copy()
    tosca_builtins['__import__'] = __import__
    tosca_builtins["__metaclass__"] = type
    namespace.update( {
            '_getattr_': default_guarded_getattr,
            '_getitem_': default_guarded_getitem,
            '_getiter_': default_guarded_getiter,
        } )

    namespace['__builtins__'] = tosca_builtins
    namespace['__name__'] = "builtins"
    result = compile_restricted_exec(python_src, policy=OwnRestrictingNodeTransformer)
    # doc strings are in here: result.used_names
    if result.errors:
        return {}
    exec(result.code, namespace)
    converter = PythonToYaml(namespace, yaml_cls, result.used_names)
    yaml_dict = converter.module2yaml()
    # XXX
    # if path:
    #     with open(path, "w") as yo:
    #         yaml.dump(yaml_dict, yo)
    return yaml_dict
