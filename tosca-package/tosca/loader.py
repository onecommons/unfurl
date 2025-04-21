# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import sys
import os.path
import logging
from pathlib import Path
from importlib.abc import Loader
from importlib import invalidate_caches
from importlib.machinery import FileFinder, ModuleSpec, PathFinder
from importlib.util import spec_from_file_location, spec_from_loader, module_from_spec
import traceback
from typing import Any, Dict, Mapping, Optional, Sequence
from types import ModuleType
import importlib._bootstrap
from _ast import AnnAssign, Assign, ClassDef, Module, With, Expr
from ast import Str, Constant, Name
import ast
import builtins
from toscaparser.imports import ImportResolver
from RestrictedPython import compile_restricted_exec, CompileResult
from RestrictedPython import RestrictingNodeTransformer
from RestrictedPython import safe_builtins
from RestrictedPython.transformer import ALLOWED_FUNC_NAMES, FORBIDDEN_FUNC_NAMES
from . import Namespace, global_state

logger = logging.getLogger("tosca")

# python standard library modules matches those added to utility_builtins
ALLOWED_MODULES = (
    "unfurl",
    "typing",
    "typing_extensions",
    "tosca",
    "tosca.scalars",
    "random",
    "math",
    "string",
    "datetime",
    "urllib.parse",
    "re",
    "base64",
    "os.path",
)

# XXX have the unfurl package set these:
ALLOWED_PRIVATE_PACKAGES = (
    "unfurl.tosca_plugins",
    "unfurl.configurators.templates",
)

__os_path_safe__ = (
    "normcase",
    "isabs",
    "join",
    "splitdrive",
    "splitroot",
    "split",
    "splitext",
    "basename",
    "dirname",
    "commonprefix",
    "normpath",
    "abspath",
    "supports_unicode_filenames",
    "relpath",
    "commonpath",
)


def get_allowed_modules(packages=()) -> Dict[str, ModuleType]:
    # make sure these ALLOWED_MODULES have been imported
    import re, base64, math, string, datetime, urllib.parse, random

    allowed: Dict[str, ModuleType] = {}
    for name in ALLOWED_MODULES + packages:
        if name in sys.modules:
            first, sep, last = name.partition(".")
            allowed[name] = ImmutableModule(name, sys.modules[name])
            if sep:
                if first in ALLOWED_MODULES:
                    allowed[first] = ImmutableModule(first, sys.modules[first])
                else:
                    kw = {last: allowed[name]}
                    allowed[first] = DeniedModule(first, (), **kw)

    for name in packages:
        # set sub packages as attributes now to prevent Python's import machinery
        # from loading different copy that won't have the sandbox leaf module set.
        first, sep, last = name.partition(".")
        parts = name.split(".")
        if len(parts) == 1:
            continue
        if len(parts) == 2:
            ImmutableModule._ImmutableModule__set_sub_module(  # type: ignore[attr-defined]
                allowed[parts[0]], parts[1], allowed[name]
            )
        else:
            assert len(parts) == 3
            ImmutableModule._ImmutableModule__set_sub_module(  # type: ignore[attr-defined]
                allowed[parts[0] + "." + parts[1]], parts[2], allowed[name]
            )
    return allowed


def get_module_path(module) -> str:
    if getattr(module, "__spec__", None) and module.__spec__.origin:
        # __file__ can be wrong
        return module.__spec__.origin
    elif module.__file__:
        return module.__file__
    else:
        assert hasattr(module, "__path__")
        module_path = module.__path__
        try:
            module_path = module_path[0]
        except TypeError:
            # _NamespacePath missing __getitem__ on older Pythons
            module_path = module_path._path[0]  # type: ignore
        return module_path


class RepositoryFinder(PathFinder):
    "Place on sys.meta_path to enable finding modules in tosca repositories"

    @classmethod
    def find_spec(cls, fullname: str, path=None, target=None, modules=None):
        # path is a list with a path to the parent package or None if no parent
        names = fullname.split(".")
        if names[0] == "tosca_repositories":
            if len(names) == 1:
                return ModuleSpec(fullname, None, is_package=True)
            if import_resolver:
                # this may clone a repository
                repo_path = import_resolver.find_repository_path(names[1])
            else:
                # assume already created
                repo_path = os.path.join(
                    service_template_basedir, "tosca_repositories", names[1]
                )
            if repo_path:
                if len(names) == 2:
                    if os.path.exists(repo_path):
                        init_path = os.path.join(repo_path, "__init__.py")
                        if os.path.exists(init_path):
                            loader = ToscaYamlLoader(fullname, init_path, modules)
                            return spec_from_file_location(
                                fullname,
                                init_path,
                                loader=loader,
                                submodule_search_locations=[repo_path],
                            )
                        else:
                            return ModuleSpec(
                                fullname, None, origin=repo_path, is_package=True
                            )
                    else:
                        logger.error(
                            f"Can't load module {fullname}: {repo_path} doesn't exist"
                        )
                        return None
                else:
                    origin_path = os.path.join(repo_path, *names[2:])
                    if os.path.isdir(origin_path) and not os.path.isfile(
                        origin_path + ".py"
                    ):
                        init_path = os.path.join(origin_path, "__init__.py")
                        if os.path.exists(init_path):
                            loader = ToscaYamlLoader(fullname, init_path, modules)
                            return spec_from_file_location(
                                fullname,
                                init_path,
                                loader=loader,
                                submodule_search_locations=[origin_path],
                            )
                        else:
                            return ModuleSpec(
                                fullname, None, origin=origin_path, is_package=True
                            )
                    else:
                        origin_path += ".py"
                    assert os.path.isfile(origin_path), origin_path
                    loader = ToscaYamlLoader(fullname, origin_path, modules)
                    spec = spec_from_loader(fullname, loader, origin=origin_path)
                    return spec
            else:
                logger.error(
                    f"Can't load module {fullname}: have you declared a repository for {names[1]}?"
                )
        # XXX special case service-template.yaml as service_template ?
        elif names[0] == "service_template":
            if path:
                try:
                    dir_path = path[0]
                except TypeError:
                    # _NamespacePath missing __getitem__ on older Pythons
                    dir_path = path._path[0]  # type: ignore
            else:
                dir_path = service_template_basedir
            if len(names) == 1:
                return ModuleSpec(fullname, None, origin=dir_path, is_package=True)
            else:
                origin_path = os.path.join(dir_path, *names[1:])
                if os.path.isdir(origin_path) and not os.path.isfile(
                    origin_path + ".py"
                ):
                    return ModuleSpec(
                        fullname, None, origin=origin_path, is_package=True
                    )
                origin_path += ".py"
                loader = ToscaYamlLoader(fullname, origin_path, modules)
                spec = spec_from_loader(fullname, loader, origin=origin_path)
                return spec
                # return PathFinder.find_spec(fullname, [dir_path], target)

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
        self.full_name: str = full_name
        self.filepath: str = filepath
        self.modules: Optional[dict] = modules

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # parse to TOSCA template and convert to python
        path = Path(self.filepath)
        if path.suffix in loader_details[1]:
            from .yaml2python import yaml_to_python

            python_filepath = str(path.parent / (path.stem + ".py"))
            src = yaml_to_python(
                self.filepath, python_filepath, import_resolver=import_resolver
            )
        else:
            python_filepath = str(path)
            with open(path) as f:
                src = f.read()
        safe_mode = (
            import_resolver.get_safe_mode()
            if import_resolver
            else global_state.safe_mode
        )
        module.__dict__["__file__"] = python_filepath
        for i in range(self.full_name.count(".")):
            path = path.parent
        restricted_exec(
            src, vars(module), str(path), self.full_name, self.modules, safe_mode
        )


class ImmutableModule(ModuleType):
    """Immutable wrapper around allowed modules.
    This is only securely immutable when accessed through safe mode, otherwise it's trivial to circumvent."""

    __always_safe__ = (
        "__safe__",
        "__all__",
        "__name__",
        "__qualname__",
        "__package__",
        "__file__",
        "__root__",
        "__module__",
    )

    def __init__(self, name, module):
        super().__init__(name)
        object.__setattr__(self, "__module", module)
        object.__setattr__(self, "__sub_modules__", {})

    def __getattribute__(self, __name: str) -> Any:
        mod_name = super().__getattribute__("__name__")
        module = super().__getattribute__("__module")
        if mod_name == "os.path":
            safe: tuple = __os_path_safe__
        else:
            safe = getattr(module, "__safe__", getattr(module, "__all__", ()))
        if (
            __name in ImmutableModule.__always_safe__
            or __name in safe
            or mod_name == "math"
        ):
            # only allow access to public attributes
            # special case "math", it doesn't have __all__
            return getattr(module, __name)
        else:
            try:
                return super().__getattribute__("__sub_modules__")[__name]
            except KeyError:
                raise AttributeError(__name)

    def __set_sub_module(self, name, v):  # called as _ImmutableModule__set_sub_module
        super().__getattribute__("__sub_modules__")[name] = v
        object.__setattr__(self, name, v)

    def __setattr__(self, name, v):
        raise AttributeError(name)

    def __delattr__(self, name):
        raise AttributeError(name)


class DeniedModule(ModuleType):
    """
    A dummy module that defers raising ImportError until the module is accessed.
    This allows unsafe import statements in the global scope as long as access is never attempted during sandbox execution.
    """

    def __init__(self, name, fromlist, **kw):
        super().__init__(name)
        object.__getattribute__(self, "__dict__").update(kw)
        object.__getattribute__(self, "__dict__")["__fromlist__"] = fromlist

    def __getattribute__(self, __name: str) -> Any:
        if __name == "path" or __name == "parse":
            # for os.path and urlib.parse in allowed modules
            return object.__getattribute__(self, __name)

        name = object.__getattribute__(self, "__name__")
        fromlist = object.__getattribute__(self, "__fromlist__")
        if fromlist and __name in fromlist:
            # the import machinery will try to access attributes on the fromlist
            # pretend it is a DeniedModule to defer ImportErrors until access
            return DeniedModule(__name, (), __package__=name)
        try:
            __package__ = object.__getattribute__(self, "__package__")
        except:
            raise ImportError("Import of " + name + " is not permitted", name=name)
        else:
            raise ImportError(
                f"Import of {name} in {__package__} is not permitted", name=name
            )

    def __setattr__(self, name, v):
        raise AttributeError(name)

    def __delattr__(self, name):
        raise AttributeError(name)


def load_private_module(base_dir: str, modules: Dict[str, ModuleType], name: str):
    parent, sep, last = name.rpartition(".")
    if parent and parent not in modules:
        load_private_module(base_dir, modules, parent)
    if name in modules:
        # cf. "Crazy side-effects!" in _bootstrap.py (e.g. parent could have imported child)
        return modules[name]
    if name.startswith("tosca_repositories") or name.startswith("service_template"):
        spec = RepositoryFinder.find_spec(name, [base_dir])
        if not spec:
            raise ModuleNotFoundError(
                f"No module named {name} (base: {base_dir})", name=name
            )
    else:
        if parent:
            parent_path = get_module_path(modules[parent])
            if os.path.isfile(parent_path):
                parent_path = os.path.dirname(parent_path)
            origin_path = os.path.join(parent_path, last)
        else:
            origin_path = os.path.join(base_dir, name.replace(".", "/"))
        if name in ALLOWED_PRIVATE_PACKAGES:
            spec = ModuleSpec(name, None, origin=origin_path, is_package=True)
        else:
            if os.path.isdir(origin_path) and not os.path.isfile(origin_path + ".py"):
                spec = ModuleSpec(name, None, origin=origin_path, is_package=True)
            else:
                origin_path += ".py"
                if not os.path.isfile(origin_path):
                    raise ModuleNotFoundError(
                        f"No module named {name} at {origin_path}", name=name
                    )
                loader = ToscaYamlLoader(name, origin_path, modules)
                spec = spec_from_loader(name, loader, origin=origin_path)
                assert spec and spec.loader
    module = module_from_spec(spec)
    modules[name] = module
    if spec.loader:
        try:
            assert spec.loader.__class__.__name__ in (
                "_NamespaceLoader",
                "NamespaceLoader",
                "ToscaYamlLoader",
            ), f"unexpected loader {spec.loader}"
            spec.loader.exec_module(module)
        except:
            del modules[name]
            raise
    if parent:
        # Set the module as an attribute on its parent.
        parent_module = modules[parent]
        child = name.rpartition(".")[2]
        if not hasattr(parent_module, child):
            if isinstance(parent_module, ImmutableModule):
                ImmutableModule._ImmutableModule__set_sub_module(  # type: ignore[attr-defined]
                    parent_module, child, module
                )
            else:
                try:
                    setattr(parent_module, child, module)
                except AttributeError:
                    msg = (
                        f"Cannot set an attribute on {parent!r} for child module {child!r}"
                    )
                    logger.warning(msg)
    return module


def _check_fromlist(module, fromlist):
    # note: allowed modules aren't lazily checked like denied modules and so will raise ImportError in safe mode
    if fromlist:
        allowed = set(getattr(module, "__safe__", getattr(module, "__all__", ())))
        for name in fromlist:
            if (
                name != "*" and name not in allowed
            ):  # and name not in _second_level_packages:
                raise ImportError(
                    f"Import of {name} from {module.__name__} is not permitted",
                    name=module.__name__,
                )


def _load_or_deny_module(name, ALLOWED_MODULES, modules):
    if name in modules:
        return modules[name]
    if name in ALLOWED_MODULES:
        module = importlib.import_module(name)
        module = ImmutableModule(name, module)
        modules[name] = module
    else:
        # don't add to modules because fromlist varies
        module = DeniedModule(name, ())
    return module


def __safe_import__(
    base_dir: str,
    ALLOWED_MODULES: Sequence[str],
    modules: Dict[str, ModuleType],
    name: str,
    globals=None,
    locals=None,
    fromlist=(),
    level=0,
):
    parts = name.split(".")
    assert modules is not sys.modules
    if level == 0:
        if name in modules:
            if not fromlist:
                return modules[parts[0]]
            module = modules[name]
            if parts[0] == "tosca_repositories" or name in ALLOWED_PRIVATE_PACKAGES:
                for from_name in fromlist:
                    if not hasattr(module, from_name):
                        # e.g. from tosca_repositories.repo import module
                        load_private_module(base_dir, modules, name + "." + from_name)
            if name in ALLOWED_MODULES:
                _check_fromlist(module, fromlist)
            # otherwise a privately loaded or DeniedModule, don't need to validate fromlist besides *
            elif "*" in fromlist:
                _validate_star(module)
            # XXX if DeniedModule, update its __fromlist__ to defer ImportError
            return module
        if name in ALLOWED_MODULES:  # allowed but need to be made ImmutableModule
            if len(parts) > 1:
                first = _load_or_deny_module(parts[0], ALLOWED_MODULES, modules)
                last = importlib.import_module(name)
                _check_fromlist(last, fromlist)
                last = ImmutableModule(name, last)
                modules[name] = last
                # we don't need to worry about _handle_fromlist here because we don't allow importing submodules
                return last if fromlist else first
            else:
                module = importlib.import_module(name)
                _check_fromlist(module, fromlist)
                module = ImmutableModule(name, module)
                modules[name] = module
                return module
        elif name not in ALLOWED_PRIVATE_PACKAGES and parts[0] != "tosca_repositories":
            package_name, sep, module_name = name.rpartition(".")
            # check if module is in an allowed package
            if package_name not in ALLOWED_PRIVATE_PACKAGES:
                if fromlist:
                    return DeniedModule(name, fromlist)
                else:
                    return _load_or_deny_module(parts[0], ALLOWED_MODULES, modules)
            # not denied, so fall through to load_private_module() below
    else:
        # relative import so load_private_module
        package = globals["__package__"] if globals else None
        importlib._bootstrap._sanity_check(name, package, level)  # type: ignore[attr-defined]
        name = importlib._bootstrap._resolve_name(name, package, level)  # type: ignore[attr-defined]

    # load user code in our restricted environment
    module = load_private_module(base_dir, modules, name)
    if fromlist:
        if "*" in fromlist:
            _validate_star(module)
        # see https://github.com/python/cpython/blob/3.11/Lib/importlib/_bootstrap.py#L1207
        importlib._bootstrap._handle_fromlist(  # type: ignore[attr-defined]
            module, fromlist, lambda name: load_private_module(base_dir, modules, name)
        )
    elif len(parts) > 1:
        return load_private_module(base_dir, modules, parts[0])
    return module


def _validate_star(module):
    # ok if there's no __all__ because default * will exclude names that start with "_"
    safe = getattr(module, "__safe__", None)
    if safe is not None:
        all_name = getattr(module, "__all__", ())
        if set(safe) != set(all_name):
            raise ImportError(
                f'Import of * from {module.__name__} is not permitted, its "__all__" does not match its "__safe__" attribute.',
                name=module.__name__,
            )


def doc_str(node):
    if isinstance(node, Expr):
        if isinstance(node.value, Constant) and isinstance(node.value.value, str):
            return node.value.value
        elif isinstance(node.value, Str):
            return str(node.value.s)
    return None


def get_descriptions(body):
    doc_strings = {}
    current_name = None
    for node in body:
        if (
            isinstance(node, Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], Name)
        ):
            current_name = node.targets[0].id
            continue
        elif isinstance(node, AnnAssign) and isinstance(node.target, Name):
            current_name = node.target.id
            continue
        elif current_name and doc_str(node):
            doc_strings[current_name] = doc_str(node)
        current_name = None
    return doc_strings


def default_guarded_getattr(ob, name):
    try:
        return getattr(ob, name)
    except AttributeError:
        raise


def default_guarded_getitem(ob, index):
    # No restrictions.
    return ob[index]


def default_guarded_getiter(ob):
    # No restrictions.
    return ob


def default_guarded_write(ob):
    # No restrictions.
    return ob


def default_guarded_apply(func, *args, **kws):
    return func(*args, **kws)


def safe_guarded_write(ob):
    # don't allow objects in the allowlist of modules to be modified
    if getattr(ob, "__module__", "").partition(".")[0] in ALLOWED_MODULES:
        # classes, functions, and methods are all callable
        if not callable(ob) and not isinstance(ob, Namespace):
            return ob
        raise TypeError(
            f"Modifying objects in {ob.__module__} is not permitted: {ob}, {type(ob)}"
        )
    return ob


# XXX
# _inplacevar_
# _iter_unpack_sequence_
# _unpack_sequence_

PRINT_AST_SRC = os.getenv("UNFURL_TEST_PRINT_AST_SRC")
FORCE_SAFE_MODE = os.getenv("UNFURL_TEST_SAFE_LOADER")


class ToscaDslNodeTransformer(RestrictingNodeTransformer):
    def __init__(self, errors=None, warnings=None, used_names=None):
        super().__init__(errors, warnings, used_names)

    def _name_ok(self, node, name) -> bool:
        return True

    def error(self, node, info):
        # visit_Attribute() checks names inline instead of calling check_name()
        # so we have to do the name check this way:
        if 'invalid attribute name because it starts with "_"' in info:
            if self._name_ok(node, node.attr):
                return
        super().error(node, info)

    def check_name(self, node, name, allow_magic_methods=False):
        if not self._name_ok(node, name):
            self.error(node, f'"{name}" is an invalid variable name"')

    def check_import_names(self, node):
        return self.node_contents_visit(node)

    def visit(self, node):
        if PRINT_AST_SRC and not ":top" in self.used_names:
            self.used_names[":top"] = node
        v = super().visit(node)
        return v

    def visit_Constant(self, node):
        # allow `...`
        return self.node_contents_visit(node)

    def visit_Ellipsis(self, node):
        # allow `...`
        # replaced by Constant in 3.8
        return self.node_contents_visit(node)

    def visit_AnnAssign(self, node: AnnAssign) -> Any:
        # missing in RestrictingNodeTransformer
        # this doesn't need the logic that is in visit_Assign
        # because it doesn't have a "targets" attribute,
        # and node.target: Name | Attribute | Subscript
        return self.node_contents_visit(node)

    # new Python 3.12 nodes
    def visit_TypeAlias(self, node) -> Any:
        # missing in RestrictingNodeTransformer
        return self.node_contents_visit(node)

    def visit_TypeVar(self, node) -> Any:
        # missing in RestrictingNodeTransformer
        return self.node_contents_visit(node)

    def visit_TypeVarTuple(self, node) -> Any:
        # missing in RestrictingNodeTransformer
        return self.node_contents_visit(node)

    def visit_ParamSpec(self, node) -> Any:
        # missing in RestrictingNodeTransformer
        return self.node_contents_visit(node)

    def visit_ClassDef(self, node: ClassDef) -> Any:
        # find attribute docs in this class definition
        doc_strings = get_descriptions(node.body)
        self.used_names[node.name + ":doc_strings"] = doc_strings
        return super().visit_ClassDef(node)


ALLOWED_FUNC_NAMES = ALLOWED_FUNC_NAMES | frozenset(ImmutableModule.__always_safe__)


class SafeToscaDslNodeTransformer(ToscaDslNodeTransformer):
    def _name_ok(self, node, name: str) -> bool:
        if not name:
            return False
        if name in FORBIDDEN_FUNC_NAMES:
            return False
        if name in ALLOWED_FUNC_NAMES:
            return True
        if name[0] == "_" and "__" in name:
            return (
                False  # deny dundernames, private methods, and private method backdoor
            )
        return True

    def check_import_names(self, node):
        if (
            isinstance(node, ast.ImportFrom)
            and len(node.names) == 1
            and node.names[0].name == "*"
        ):
            return self.node_contents_visit(node)
        else:
            # import * is not allowed (to avoid rebinding attacks)
            return RestrictingNodeTransformer.check_import_names(self, node)


loader_details = ToscaYamlLoader, [".yaml", ".yml"]
installed = False
import_resolver: Optional[ImportResolver] = None
service_template_basedir = ""


def _clear_private_modules():
    for name in list(sys.modules):
        if name.startswith("service_template") or name.startswith("tosca_repositories"):
            del sys.modules[name]


def install(import_resolver_: Optional[ImportResolver], base_dir=None) -> str:
    # insert the path hook ahead of other path hooks
    global import_resolver
    import_resolver = import_resolver_
    global service_template_basedir
    old_basedir = service_template_basedir
    if base_dir:
        service_template_basedir = base_dir
    else:
        service_template_basedir = os.getcwd()
    if service_template_basedir != old_basedir:
        # delete modules whose contents are relative to the base dir that has changed
        _clear_private_modules()
    global installed
    if installed:
        return old_basedir

    if not isinstance(sys.meta_path[0], RepositoryFinder):
        sys.meta_path.insert(0, RepositoryFinder())
    # XXX this breaks imports in local scope somehow:
    # sys.path_hooks.insert(0, FileFinder.path_hook(loader_details))
    # this break some imports:
    # clear any loaders that might already be in use by the FileFinder
    # sys.path_importer_cache.clear()
    # invalidate_caches()
    installed = True
    return old_basedir


class PrintCollector:
    # based on RestrictedPython/PrintCollector.py but make it actually print something
    """Collect written text, and return it when called."""

    def __init__(self, _getattr_=None):
        self.txt = []
        self._getattr_ = _getattr_

    def write(self, text):
        self.txt.append(text)

    def __call__(self):
        # not sure when this called?
        return "".join(self.txt)

    def _call_print(self, *objects, **kwargs):
        if kwargs.get("file", None) is None:
            kwargs["file"] = self
        else:
            self._getattr_(kwargs["file"], "write")
        # print(*object) doesn't work but this does:
        sys.stdout.write(" ".join(str(o) for o in objects))


def get_safe_mode(current_safe_mode) -> bool:
    if FORCE_SAFE_MODE == "never":
        return False
    elif FORCE_SAFE_MODE and not current_safe_mode:
        return True
    return current_safe_mode


def restricted_exec(
    python_src: str,
    namespace,
    base_dir: str,
    full_name="service_template",
    modules=None,
    safe_mode=False,
) -> CompileResult:
    # package is the full name of module
    # path is base_dir to the root of the package
    safe_mode = get_safe_mode(safe_mode)
    package, sep, module_name = full_name.rpartition(".")
    if modules is None:
        modules = global_state.modules if safe_mode else sys.modules

    if namespace is None:
        namespace = {}
    tosca_builtins = safe_builtins.copy()
    # https://docs.python.org/3/library/functions.html?highlight=__import__#import__
    safe_import = lambda *args: __safe_import__(
        base_dir, ALLOWED_MODULES, modules, *args
    )
    tosca_builtins["__import__"] = safe_import if safe_mode else __import__
    # we don't restrict read access so add back the safe builtins
    # missing from safe_builtins, only exclude the following:
    # "aiter", "anext", "breakpoint", "compile", "delattr", "dir", "eval", "exec", "exit", "quit", "print"
    # "globals", "locals", "open", input, setattr, vars, license, copyright, help, credits
    for name in [
        "NotImplemented",
        "all",
        "any",
        "ascii",
        "bin",
        "bytearray",
        "classmethod",
        "dict",
        "enumerate",
        "filter",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "iter",
        "list",
        "map",
        "max",
        "memoryview",
        "min",
        "next",
        "object",
        "property",
        "reversed",
        "set",
        "staticmethod",
        "sum",
        "super",
        "type",
    ]:
        tosca_builtins[name] = getattr(builtins, name)
    namespace.update({
        "_getattr_": default_guarded_getattr,
        "_getitem_": default_guarded_getitem,
        "_getiter_": default_guarded_getiter,
        "_apply_": default_guarded_apply,
        "_write_": safe_guarded_write if safe_mode else default_guarded_write,
        "_print_": PrintCollector,
        "__metaclass__": type,
    })
    namespace["__builtins__"] = tosca_builtins
    namespace["__name__"] = full_name
    if base_dir and "__file__" not in namespace:
        # try to guess file path from module name
        if full_name.startswith("service_template."):
            # assume service_template is just our dummy package
            module_name = full_name[len("service_template.") :]
        else:
            module_name = full_name
        namespace["__file__"] = (
            os.path.join(base_dir, module_name.lstrip(".").replace(".", "/")) + ".py"
        )
    if package and "__package__" not in namespace:
        namespace["__package__"] = package
    policy = SafeToscaDslNodeTransformer if safe_mode else ToscaDslNodeTransformer
    # print(python_src)
    result = compile_restricted_exec(python_src, policy=policy)
    if PRINT_AST_SRC and sys.version_info.minor >= 9:
        c_ast = result.used_names[":top"]
        print(ast.unparse(c_ast))  # type: ignore
    if result.errors:
        file_name = namespace.get("__file__", full_name)
        raise SyntaxError(file_name + " " + "\n".join(result.errors))
    if full_name not in modules:
        temp_module = ModuleType(full_name)
        temp_module.__dict__.update(namespace)
        if full_name == "service_template":
            temp_module.__path__ = [service_template_basedir]
        modules[full_name] = temp_module
    else:
        temp_module = modules[full_name]
        temp_module.__dict__.update(namespace)
    remove_temp_module = False
    if full_name not in sys.modules:
        # dataclass._process_class() might assume the current module is in sys.modules
        # so to make it happy add a dummy one if its missing
        sys.modules[full_name] = temp_module
        remove_temp_module = True
    previous_safe_mode = global_state.safe_mode
    previous_mode = global_state.mode
    try:
        global_state.safe_mode = safe_mode
        global_state.mode = "parse"
        global_state.modules = modules
        if temp_module:
            exec(result.code, temp_module.__dict__)
            namespace.update(temp_module.__dict__)
        else:
            exec(result.code, namespace)
    finally:
        global_state.safe_mode = previous_safe_mode
        global_state.mode = previous_mode
        if remove_temp_module:
            del sys.modules[full_name]
    return result
