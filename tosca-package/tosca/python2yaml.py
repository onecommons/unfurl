# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import importlib, importlib.util, importlib._bootstrap
import io
import inspect
from types import ModuleType
import sys
import os.path
from typing import (
    Any,
    Dict,
    Set,
    List,
    Optional,
    Tuple,
    Union,
)
import logging

logger = logging.getLogger("tosca")
from pathlib import Path
from toscaparser import topology_template
from ._tosca import (
    _DataclassType,
    ToscaType,
    RelationshipType,
    NodeType,
    CapabilityType,
    global_state,
    WritePolicy,
)
from .loader import restricted_exec, get_module_path


class PythonToYaml:
    def __init__(
        self,
        namespace: dict,
        yaml_cls=dict,
        docstrings=None,
        safe_mode=False,
        modules=None,
        write_policy=WritePolicy.never,
        import_resolver=None,
    ):
        self.globals = namespace
        self.imports: Set[Tuple[str, Path]] = set()
        self.repos: Dict[str, Path] = {}
        self.yaml_cls = yaml_cls
        self.sections: Dict[str, Any] = yaml_cls(topology_template=yaml_cls())
        self.docstrings = docstrings or {}
        self.safe_mode = safe_mode
        if modules is None:
            self.modules = {} if safe_mode else sys.modules
        else:
            self.modules = modules
        self.write_policy = write_policy
        self.import_resolver = import_resolver

    def find_yaml_import(
        self, module_name: str
    ) -> Tuple[Optional[ModuleType], Optional[Path]]:
        module = self.modules.get(module_name) or sys.modules.get(module_name)
        if not module:
            return None, None
        path = module.__file__
        assert path
        dirname, filename = os.path.split(path)
        before, sep, remainder = filename.rpartition(".")
        glob = before.replace("_", "?") + ".*"
        for p in Path(dirname).glob(glob):
            if p.suffix in [".yaml", ".yml"]:
                return module, p
        return module, None

    def _set_repository_for_module(
        self, module_name: str, path: Path
    ) -> Tuple[str, Optional[Path]]:
        parts = module_name.split(".")
        if parts[0] == "tosca_repositories":
            root_package = parts[0] + "." + parts[1]
            repo_name = parts[1]
        else:
            root_package = parts[0]
            repo_name = parts[0]
        root_module = self.modules.get(root_package, sys.modules.get(root_package))
        if not root_module:
            return "", None
        root_path = root_module.__file__
        if not root_path:
            root_path = get_module_path(root_module)
            repo_path = Path(root_path)
        else:
            repo_path = Path(root_path).parent
        self.repos[repo_name] = repo_path
        return repo_name, path.relative_to(repo_path)

    def module2yaml(self) -> dict:
        mode = global_state.mode
        try:
            global_state.mode = "yaml"
            self._namespace2yaml(self.globals)
        finally:
            global_state.mode = mode
        self.add_repositories_and_imports()
        return self.sections

    def add_repositories_and_imports(self) -> None:
        imports = []
        repositories = {}
        for repo, p in self.imports:
            _import = dict(file=str(p))
            if repo:
                _import["repository"] = repo
                if not self.import_resolver or not self.import_resolver.get_repository(
                    repo, None
                ):
                    # if repository has already been defined, don't add it here (it will probably be wrong)
                    repositories[repo] = dict(url=self.repos[repo].as_uri())
            imports.append(_import)
        if repositories:
            self.sections.setdefault("repositories", {}).update(repositories)
        if imports:
            self.sections.setdefault("imports", []).extend(imports)

    def add_template(self, obj: ToscaType, name: str = "") -> str:
        section = self.sections["topology_template"].setdefault(
            obj._template_section, self.yaml_cls()
        )
        name = obj._name or name
        if name not in section:
            section[name] = obj  # placeholder to prevent circular references
            section[name] = obj.to_template_yaml(self)
        return name

    def set_requirement_value(self, req: dict, value, default_node_name: str):
        node = None
        if isinstance(value, NodeType):
            node = value
        elif isinstance(value, CapabilityType):
            if value._local_name:
                node = value._node
                req["capability"] = value._local_name
            else:
                req["capability"] = value.tosca_type_name()
        elif isinstance(value, RelationshipType):
            if value._name:  # named, not inline
                rel: Union[str, dict] = self.add_template(value)
            else:  # inline
                rel = value.to_template_yaml(self)
            req = self.yaml_cls(relationship=rel)
            if isinstance(value._target, NodeType):
                node = value._target
            elif isinstance(value._target, CapabilityType):
                node = value._target._node
                req["capability"] = value._target._local_name
        if node:
            node_name = self.add_template(node, default_node_name)
            req["node"] = node_name
            if len(req) == 1:
                return node_name
        return None

    def _imported_module2yaml(self, module: ModuleType) -> Path:
        try:
            from unfurl.yamlloader import yaml
        except ImportError:
            import yaml

        path = Path(get_module_path(module))
        yaml_path = path.parent / (path.stem + ".yaml")
        if not self.write_policy.can_overwrite(module.__file__, str(yaml_path)):
            logger.info(
                "skipping saving imported python module as YAML %s: %s",
                yaml_path,
                self.write_policy.deny_message(),
            )
            return yaml_path

        base_dir = "/".join(path.parts[1 : -len(module.__name__.split("."))])
        with open(path) as sf:
            src = sf.read()
        yaml_dict = python_src_to_yaml_obj(
            src,
            None,
            base_dir,
            module.__name__,
            self.yaml_cls,
            self.safe_mode,
            self.modules,
            self.write_policy,
            self.import_resolver,
        )
        # if self.import_resolver:
        #     yaml = self.import_resolver.manifest.config.yaml
        with open(yaml_path, "w") as yo:
            logger.info(
                "saving imported python module as YAML at %s %s",
                yaml_path,
                type(yaml_dict),
            )
            yaml.dump(yaml_dict, yo)
        return yaml_path

    def _namespace2yaml(self, namespace):
        current_module = self.globals.get(
            "__name__", "builtins"
        )  # exec() adds to builtins
        path = self.globals.get("__file__")
        description = self.globals.get("__doc__")
        self.sections["tosca_definitions_version"] = "tosca_simple_unfurl_1_0_0"
        if description and description.strip():
            self.sections["description"] = description.strip()
        topology_sections: Dict[str, Any] = self.sections["topology_template"]

        if not isinstance(namespace, dict):
            names = getattr(namespace, "__all__", None)
            if names is None:
                names = dir(namespace)
            namespace = {name: getattr(namespace, name) for name in names}

        for name, obj in namespace.items():
            if isinstance(obj, ModuleType):
                continue
            if hasattr(obj, "get_defs"):  # class Namespace
                self._namespace2yaml(obj.get_defs())
                continue
            module_name: str = getattr(obj, "__module__", "")
            if isinstance(obj, _DataclassType) and issubclass(obj, ToscaType):
                if module_name and module_name != current_module:
                    if not module_name.startswith("tosca."):
                        # logger.debug(
                        #     f"adding import statement to {current_module} for {obj} in {module_name}"
                        # )
                        # this type was imported from another module
                        # instead of converting the type, add an import if missing
                        module, p = self.find_yaml_import(module_name)
                        if not p and module:
                            #  its a TOSCA object but no yaml file found, convert to yaml now
                            p = self._imported_module2yaml(module)
                        if p and path:
                            try:
                                self.imports.add(("", p.relative_to(path)))
                            except ValueError:
                                # not a subpath of the current module, add a repository
                                ns, path = self._set_repository_for_module(
                                    module_name, p
                                )
                                if path:
                                    self.imports.add((ns, path))
                                else:
                                    logger.warning(
                                        f"import look up in {current_module} failed, can find {module_name}"
                                    )
                    continue

                # this is a class not an instance
                section = obj._type_section  # type: ignore
                obj._globals = self.globals  # type: ignore
                _docstrings = self.docstrings.get(name)
                if isinstance(_docstrings, dict):
                    obj._docstrings = _docstrings  # type: ignore
                as_yaml = obj._cls_to_yaml(self)  # type: ignore
                self.sections.setdefault(section, self.yaml_cls()).update(as_yaml)
            elif isinstance(obj, ToscaType):
                # XXX this will render any templates that were imported into this namespace from another module
                self.add_template(obj, name)
            else:
                section = getattr(obj, "_template_section", "")
                to_yaml = getattr(obj, "to_yaml", None)
                if section:
                    assert to_yaml
                    parent = self.sections
                    if section in topology_template.SECTIONS:
                        parent = topology_sections
                    parent.setdefault(section, self.yaml_cls()).update(
                        to_yaml(self.yaml_cls)
                    )


def python_src_to_yaml_obj(
    python_src: str,
    namespace: Optional[Dict[str, Any]] = None,
    base_dir: str = "",
    full_name: str = "service_template",
    yaml_cls=dict,
    safe_mode: bool = False,
    modules=None,
    write_policy=WritePolicy.never,
    import_resolver=None,
) -> dict:
    if modules is None:
        modules = {}
    if namespace is None:
        namespace = {}
    result = restricted_exec(
        python_src, namespace, base_dir, full_name, modules, safe_mode
    )
    doc_strings = {
        n[: -len(":doc_strings")]: ds
        for n, ds in result.used_names.items()
        if n.endswith(":doc_strings")
    }
    converter = PythonToYaml(
        namespace,
        yaml_cls,
        doc_strings,
        safe_mode,
        modules,
        write_policy,
        import_resolver,
    )
    yaml_dict = converter.module2yaml()
    return yaml_dict


def python_to_yaml(
    src_path: str,
    dest_path: Optional[str] = None,
    overwrite="auto",
    safe_mode: bool = False,
) -> Optional[dict]:
    try:
        from unfurl.yamlloader import yaml
    except ImportError:
        import yaml
    write_policy = WritePolicy[overwrite]
    if dest_path and not write_policy.can_overwrite(src_path, dest_path):
        logger.info(
            "not saving YAML file at %s: %s", dest_path, write_policy.deny_message()
        )
        return None
    with open(src_path) as f:
        python_src = f.read()
    base_dir = os.path.dirname(src_path)
    # add to sys.path so relative imports work
    sys.path.insert(0, base_dir)
    try:
        namespace: dict = {}
        tosca_tpl = python_src_to_yaml_obj(
            python_src,
            namespace,
            src_path,
            write_policy=write_policy,
            safe_mode=safe_mode,
        )
    finally:
        try:
            sys.path.pop(sys.path.index(base_dir))
        except ValueError:
            pass
    prologue = write_policy.generate_comment("tosca.python2yaml", src_path)
    if dest_path:
        output = io.StringIO(prologue)
        yaml.dump(tosca_tpl, output)
        logger.info("converted Python to YAML at %s", dest_path)
        with open(dest_path, "w") as f:
            f.write(output.getvalue())
    else:
        print(prologue)
        yaml.dump(tosca_tpl, sys.stdout)
    return tosca_tpl
