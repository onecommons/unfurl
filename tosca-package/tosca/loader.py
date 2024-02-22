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
from typing import Any, Dict, Optional, Sequence
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


def get_module_path(module) -> str:
    if getattr(module, "__spec__", None) and module.__spec__.origin:
        # __file__ can be wrong
        return module.__spec__.origin
    elif module.__file__:
        return module.__file__
    else:
        assert hasattr(module, "__path__"), module.__dict__
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
                        return ModuleSpec(
                            fullname, None, origin=origin_path, is_package=True
                        )
                    origin_path += ".py"
                    assert os.path.isfile(origin_path), origin_path
                    loader = ToscaYamlLoader(fullname, origin_path, modules)
                    spec = spec_from_loader(fullname, loader, origin=origin_path)
                    return spec
                    # return PathFinder.find_spec(fullname, [repo_path], target)
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
            src = yaml_to_python(self.filepath, python_filepath)
        else:
            python_filepath = str(path)
            with open(path) as f:
                src = f.read()
        safe_mode = import_resolver.get_safe_mode() if import_resolver else True
        module.__dict__["__file__"] = python_filepath
        for i in range(self.full_name.count(".")):
            path = path.parent
        restricted_exec(
            src, vars(module), path, self.full_name, self.modules, safe_mode
        )


class ImmutableModule(ModuleType):
    __always_safe__ = (
        "__safe__",
        "__all__",
        "__name__",
        "__package__",
        "__file__",
        "__root__",
    )

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
        try:
            __package__ = object.__getattribute__(self, "__package__")
        except:
            raise ImportError("Import of " + name + " is not permitted", name=name)
        else:
            raise ImportError(
                f"Import of {name} in {__package__} is not permitted", name=name
            )


def load_private_module(base_dir: str, modules: Dict[str, ModuleType], name: str):
    parent, sep, last = name.rpartition(".")
    if parent:
        if parent not in modules:
            if parent in sys.modules:
                modules[parent] = sys.modules[parent]
            else:
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
    if not spec.loader:
        return module
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
            try:
                setattr(parent_module, child, module)
            except AttributeError:
                msg = (
                    f"Cannot set an attribute on {parent!r} for child module {child!r}"
                )
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
    modules: Dict[str, ModuleType],
    name: str,
    globals=None,
    locals=None,
    fromlist=(),
    level=0,
):
    parts = name.split(".")
    if level == 0:
        is_allowed_package = name in ALLOWED_PRIVATE_PACKAGES
        if name in modules and not (is_allowed_package and fromlist):
            # we need to skip the second check to allow the fromlist to includes sub-packages
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
        elif not is_allowed_package and parts[0] not in ["tosca_repositories"]:
            # these modules fall through to load_private_module():
            package_name, sep, module_name = name.rpartition(".")
            if package_name not in ALLOWED_PRIVATE_PACKAGES:
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
        if "*" in fromlist:
            # ok if there's no __all__ because default * will exclude names that start with "_"
            safe = getattr(module, "__safe__", None)
            if safe is not None:
                all_name = getattr(module, "__all__", ())
                if set(safe) != set(all_name):
                    raise ImportError(
                        f'Import of * from {module.__name__} is not permitted, its "__all__" does not match its "__safe__" attribute.',
                        name=module.__name__,
                    )

        # see https://github.com/python/cpython/blob/3.11/Lib/importlib/_bootstrap.py#L1207
        importlib._bootstrap._handle_fromlist(
            module, fromlist, lambda name: load_private_module(base_dir, modules, name)
        )
    return module


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
        if isinstance(node, AnnAssign) and isinstance(node.target, Name):
            current_name = node.target.id
            continue
        elif current_name and doc_str(node):
            doc_strings[current_name] = doc_str(node)
        current_name = None
    return doc_strings


# python standard library modules matches those added to utility_builtins
ALLOWED_MODULES = (
    "typing",
    "typing_extensions",
    "tosca",
    "random",
    "math",
    "string",
    "DateTime",
    "unfurl",
    "urllib.parse",
)

# XXX have the unfurl package set these:
ALLOWED_PRIVATE_PACKAGES = [
    "unfurl.tosca_plugins",
    "unfurl.configurators.templates",
]


def default_guarded_getattr(ob, name):
    return getattr(ob, name)


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

    def _name_ok(self, node, name):
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
    def _name_ok(self, node, name: str):
        if not name:
            return False
        if name in FORBIDDEN_FUNC_NAMES:
            return False
        # don't allow dundernames
        if (
            name.startswith("__")
            and name.endswith("__")
            and name not in ALLOWED_FUNC_NAMES
        ):
            return False
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


def install(import_resolver_: Optional[ImportResolver], base_dir=None):
    # insert the path hook ahead of other path hooks
    global import_resolver
    import_resolver = import_resolver_
    global service_template_basedir
    if base_dir:
        service_template_basedir = base_dir
    elif not service_template_basedir:
        service_template_basedir = os.getcwd()
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


def restricted_exec(
    python_src: str,
    namespace,
    base_dir,
    full_name="service_template",
    modules=None,
    safe_mode=False,
) -> CompileResult:
    # package is the full name of module
    # path is base_dir to the root of the package
    if FORCE_SAFE_MODE == "never":
        safe_mode = False
    elif FORCE_SAFE_MODE and not safe_mode:
        safe_mode = True
        if FORCE_SAFE_MODE in ["warn", "stacktrace"]:
            logger.warning(
                "Forcing safe mode for the TOSCA loader.",
                stack_info=FORCE_SAFE_MODE == "stacktrace",
            )
    package, sep, module_name = full_name.rpartition(".")
    if modules is None:
        modules = {} if safe_mode else sys.modules

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
    namespace.update(
        {
            "_getattr_": default_guarded_getattr,
            "_getitem_": default_guarded_getitem,
            "_getiter_": default_guarded_getiter,
            "_apply_": default_guarded_apply,
            "_write_": safe_guarded_write if safe_mode else default_guarded_write,
            "_print_": PrintCollector,
            "__metaclass__": type,
        }
    )
    namespace["__builtins__"] = tosca_builtins
    namespace["__name__"] = full_name
    if base_dir and "__file__" not in namespace:
        namespace["__file__"] = (
            os.path.join(base_dir, full_name.replace(".", "/")) + ".py"
        )
    if package:
        namespace["__package__"] = package
    policy = SafeToscaDslNodeTransformer if safe_mode else ToscaDslNodeTransformer
    # print(python_src)
    result = compile_restricted_exec(python_src, policy=policy)
    if PRINT_AST_SRC and sys.version_info.minor >= 9:
        c_ast = result.used_names[":top"]
        print(ast.unparse(c_ast))  # type: ignore
    if result.errors:
        raise SyntaxError("\n".join(result.errors))
    temp_module = None
    if full_name not in sys.modules:
        # dataclass._process_class() might assume the current module is in sys.modules
        # so to make it happy add a dummy one if its missing
        temp_module = ModuleType(full_name)
        temp_module.__dict__.update(namespace)
        sys.modules[full_name] = temp_module
    previous_safe_mode = global_state.safe_mode
    previous_mode = global_state.mode
    try:
        global_state.safe_mode = safe_mode
        global_state.mode = "spec"
        global_state.modules = modules
        if temp_module:
            exec(result.code, temp_module.__dict__)
            namespace.update(temp_module.__dict__)
        else:
            exec(result.code, namespace)
    finally:
        global_state.safe_mode = previous_safe_mode
        global_state.mode = previous_mode
        if safe_mode and temp_module:
            del sys.modules[full_name]
    return result
