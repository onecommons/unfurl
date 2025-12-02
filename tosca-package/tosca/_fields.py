from typing import (
    Any,
    Dict,
    Sequence,
    Union,
    List,
    Optional,
    Type,
    TypeVar,
    Tuple,
    cast,
    overload,
    Iterable,
)
import types
import inspect
import re
import copy
from typing_extensions import (
    Callable,
    Literal,
)
from ._tosca import (
    DataConstraint,
    JsonType,
    ToscaFieldType,
    _Tosca_Field,
    MISSING,
    REQUIRED,
    CONSTRAINED,
    EvalData,
    Node,
    Relationship,
    CapabilityEntity,
)
from toscaparser.nodetemplate import NodeTemplate


class Options:
    """
    A utility class to enable structured and validated metadata on TOSCA fields.
    Options are passed to the field specifier functions and merged with unstructured metadata.
    The user can use the | operator to merge Options together.
    """

    def __init__(self, data: Dict[str, JsonType]):
        """
        Args:
            data (Dict[str, JsonType]): Metadata to be add to the field specifier.
        """
        self.data = data
        self.next: Optional[Options] = None

    def validate(self, field: "_Tosca_Field") -> Tuple[bool, str]:
        """
        This is called when initializing the field these options were passed to.
        The field's metadata will have already been set, including the data in this Options instance.

        Args:
            field (_Tosca_Field): The field that these Options has been assigned to.

        Returns:
            Tuple[bool, str]: Whether validation succeeded and an optional error message if it didn't.
        """
        return True, ""

    def set_options(self, field: "_Tosca_Field"):
        metadata = field.metadata.copy()
        option: Optional[Options] = self
        while option:
            metadata.update(option.data)
            option = option.next
        field.metadata = types.MappingProxyType(metadata)

        option = self
        while option:
            valid, msg = option.validate(field)
            if not valid:
                raise ValueError(
                    f'Invalid option for field "{field.name}": {option.data}. {msg}'
                )
            option = option.next

    def __or__(self, __value: Union["Options", dict]) -> "Options":
        if isinstance(__value, dict):
            __value = Options(__value)
        elif not isinstance(__value, Options):
            return NotImplemented
        new = copy.copy(self)
        new.next = __value
        return new

    def __ror__(self, __value: Union["Options", dict]) -> "Options":
        if isinstance(__value, dict):
            __value = Options(__value)
        elif not isinstance(__value, Options):
            return NotImplemented
        new = copy.copy(self)
        new.next = __value
        return new


class PropertyOptions(Options):
    "An option that only applies to TOSCA properties."
    def validate(self, field: "_Tosca_Field") -> Tuple[bool, str]:
        return (
            field.tosca_field_type == ToscaFieldType.property,
            "This option only works with properties.",
        )


class AttributeOptions(Options):
    "An option that only applies to TOSCA attributes."
    def validate(self, field: "_Tosca_Field") -> Tuple[bool, str]:
        return (
            field.tosca_field_type == ToscaFieldType.attribute,
            "This option only works with attributes.",
        )


def _make_field_doc(func, status=False, extra: Sequence[str] = ()) -> None:
    name = func.__name__.lower()
    doc = f"""Field specifier for declaring a TOSCA {name}.

    Args:
        default (Any, optional): Default value. Set to None if the {name} isn't required. Defaults to MISSING.
        factory (Callable, optional): Factory function to initialize the {name} with a unique value per template. Defaults to MISSING.
        name (str, optional): TOSCA name of the field, overrides the {name}'s name when generating YAML. Defaults to "".
        metadata (Dict[str, JSON], optional): Dictionary of metadata to associate with the {name}.
        options (Options, optional): Additional typed metadata to merge into metadata.\n"""
    indent = "        "
    if status:
        doc += f"{indent}constraints (List[`DataConstraint`], optional): List of TOSCA property constraints to apply to the {name}.\n"
        doc += f"{indent}title (str, optional): Human-friendly alternative name of the {name}.\n"
        doc += f"{indent}status (str, optional): TOSCA status of the {name}.\n"
    for arg in extra:
        doc += f"{indent}{arg}\n"
    func.__doc__ = doc


# cf @overloads here: https://github.com/python/typeshed/blob/main/stdlib/dataclasses.pyi#L159

_T = TypeVar("_T")


@overload
def Attribute(
    *,
    default: _T,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> _T: ...


@overload
def Attribute(
    *,
    factory: Callable[[], _T],
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> _T: ...


@overload
def Attribute(
    *,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> Any: ...


def Attribute(
    *,
    default=None,
    factory=MISSING,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> Any:
    return _Tosca_Field(
        ToscaFieldType.attribute,
        default,
        factory,
        name,
        metadata,
        title,
        status,
        constraints=constraints,
        options=options,
    )


_make_field_doc(Attribute, True)


@overload
def Property(
    *,
    default: _T,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    attribute: bool = False,
) -> _T: ...


@overload
def Property(
    *,
    factory: Callable[[], _T],
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    attribute: bool = False,
) -> _T: ...


@overload
def Property(
    *,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    attribute: bool = False,
) -> Any: ...


def Property(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title: str = "",
    status: str = "",
    options: Optional[Options] = None,
    attribute: bool = False,
) -> Any:
    return _Tosca_Field(
        ToscaFieldType.property,
        default=default,
        default_factory=factory,
        name=name,
        metadata=metadata,
        title=title,
        status=status,
        constraints=constraints,
        options=options,
        declare_attribute=attribute,
    )


_make_field_doc(
    Property,
    True,
    [
        "attribute (bool, optional): Indicate that the property is also a TOSCA attribute. Defaults to False."
    ],
)

RT = TypeVar("RT")


def Computed(
    name="",
    *,
    factory: Callable[..., RT],
    metadata: Optional[Dict[str, JsonType]] = None,
    title: str = "",
    status: str = "",
    options: Optional["Options"] = None,
    attribute: bool = False,
) -> RT:
    """Field specifier for declaring a TOSCA property whose value is computed by the factory function at runtime.

    Args:
        factory (function): function called at runtime every time the property is evaluated.
        name (str, optional): TOSCA name of the field, overrides the Python name when generating YAML.
        metadata (Dict[str, JSON], optional): Dictionary of metadata to associate with the property.
        title (str, optional): Human-friendly alternative name of the property.
        status (str, optional): TOSCA status of the property.
        options (Options, optional): Typed metadata to apply.
        attribute (bool, optional): Indicate that the property is also a TOSCA attribute.

    Return type:
        The return type of the factory function (should be compatible with the field type).
    """
    if hasattr(factory, "_is_template_function"):
        default: Any = MISSING
    else:
        default = EvalData({
            "eval": dict(computed=f"{factory.__module__}:{factory.__qualname__}")
        })
        factory = MISSING  # type: ignore
    # casting this to the factory function's return type enables the type checker to check that the return type matches the field's type
    return cast(
        RT,
        _Tosca_Field(
            ToscaFieldType.property,
            default=default,
            default_factory=factory,
            name=name,
            metadata=metadata,
            title=title,
            status=status,
            options=options,
            declare_attribute=attribute,
        ),
    )


@overload
def Requirement(
    *,
    default: _T,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    relationship: Union[str, Type["Relationship"], None] = None,
    capability: Union[str, Type["CapabilityEntity"], None] = None,
    node: Union[str, Type["Node"], None] = None,
    node_filter: Optional[Dict[str, Any]] = None,
) -> _T: ...


@overload
def Requirement(
    *,
    factory: Callable[[], _T],
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    relationship: Union[str, Type["Relationship"], None] = None,
    capability: Union[str, Type["CapabilityEntity"], None] = None,
    node: Union[str, Type["Node"], None] = None,
    node_filter: Optional[Dict[str, Any]] = None,
) -> _T: ...


@overload
def Requirement(
    *,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    relationship: Union[str, Type["Relationship"], None] = None,
    capability: Union[str, Type["CapabilityEntity"], None] = None,
    node: Union[str, Type["Node"], None] = None,
    node_filter: None = None,
) -> Any: ...


@overload
def Requirement(
    *,
    default: Any = CONSTRAINED,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    relationship: Union[str, Type["Relationship"], None] = None,
    capability: Union[str, Type["CapabilityEntity"], None] = None,
    node: Union[str, Type["Node"], None] = None,
    node_filter: Dict[str, Any],
) -> Any: ...


def Requirement(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    relationship: Union[str, Type["Relationship"], None] = None,
    capability: Union[str, Type["CapabilityEntity"], None] = None,
    node: Union[str, Type["Node"], None] = None,
    node_filter: Optional[Dict[str, Any]] = None,
) -> Any:
    field: Any = _Tosca_Field(
        ToscaFieldType.requirement,
        default,
        factory,
        name,
        metadata,
        options=options,
    )
    field.relationship = relationship
    field.capability = capability
    field.node = node
    field.node_filter = node_filter
    if node_filter:
        NodeTemplate.validate_nodefilter(node_filter, f"requirement {name}")
    if node_filter or node or capability or relationship:
        if field.default == REQUIRED or (
            field.default == MISSING and field.default_factory == MISSING
        ):
            field.default = CONSTRAINED
    return field


_make_field_doc(
    Requirement,
    False,
    [
        "relationship (str | Type[Relationship], optional): The requirement's ``relationship`` specified by TOSCA type name or Relationship class.",
        "capability (str | Type[CapabilityEntity], optional): The requirement's ``capability`` specified by TOSCA type name or CapabilityEntity class.",
        "node (str, | Type[Node], optional): The requirement's ``node`` specified by TOSCA type name or Node class.",
        "node_filter (Dict[str, Any], optional): The TOSCA node_filter for this requirement.",
    ],
)


@overload
def Capability(
    *,
    default: _T,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    valid_source_types: Optional[List[str]] = None,
) -> _T: ...


@overload
def Capability(
    *,
    factory: Callable[[], _T],
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    valid_source_types: Optional[List[str]] = None,
) -> _T: ...


@overload
def Capability(
    *,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    valid_source_types: Optional[List[str]] = None,
) -> Any: ...


def Capability(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
    valid_source_types: Optional[List[str]] = None,
) -> Any:
    field: Any = _Tosca_Field(
        ToscaFieldType.capability,
        default,
        factory,
        name,
        metadata,
        options=options,
    )
    field.valid_source_types = valid_source_types or []
    return field


_make_field_doc(
    Capability,
    False,
    [
        "valid_source_types (List[str], optional): List of TOSCA type names to set as the capability's valid_source_types"
    ],
)


@overload
def Artifact(
    *,
    default: _T,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
) -> _T: ...


@overload
def Artifact(
    *,
    factory: Callable[[], _T],
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
) -> _T: ...


@overload
def Artifact(
    *,
    name: str = "",
    metadata: Optional[Dict[str, JsonType]] = None,
    options: Optional["Options"] = None,
) -> Any: ...


def Artifact(
    *,
    default=MISSING,
    factory=MISSING,
    name="",
    metadata=None,
    options: Optional["Options"] = None,
) -> Any:
    return _Tosca_Field(
        ToscaFieldType.artifact, default, factory, name, metadata, options=options
    )


_make_field_doc(Artifact)


@overload
def Output(
    *,
    default: _T,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    mapping: Union[str, List[str]] = "",
) -> _T: ...


@overload
def Output(
    *,
    factory: Callable[[], _T],
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    mapping: Union[str, List[str]] = "",
) -> _T: ...


@overload
def Output(
    *,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    mapping: Union[str, List[str]] = "",
) -> Any: ...


def Output(
    *,
    default=None,
    factory=MISSING,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, JsonType]] = None,
    title="",
    status="",
    options: Optional[Options] = None,
    mapping: Union[str, List[str]] = "",
) -> Any:
    return _Tosca_Field(
        ToscaFieldType.attribute,
        default,
        factory,
        name,
        metadata,
        title,
        status,
        constraints=constraints,
        options=options,
    )


_F = TypeVar("_F", bound=Callable[..., Any], covariant=False)


def _strip_expr(expr) -> str:
    return str(expr).lstrip("{").rstrip("}")


def jinja_template(
    _func: Optional[_F] = None, *, convert_to: Literal["yaml", "json", None] = None
) -> Any:
    '''
    Use this decorator on methods or functions passed to `Computed` properties.
    They should return a Python f-string, which the decorator converts to a jinja2 template at runtime.
    This allows you to create a template with conditionals and loops while taking advantage of static type checking and IDE support for f-strings.

    It can be used as a plain decorator (``@jinja_template``) or with options: ``@jinja_template(convert_to="yaml")`` or ``@jinja_template(convert_to="json")``.


    Args:
      convert_to: Literal["yaml", "json", None]
        If provided, wraps the generated template in a Jinja2 filter block so the rendered text is parsed into a native Python object using the corresponding from_yaml or from_json filter.
    Returns:
      Any:
        At runtime: The value obtained by rendering the Jinja2 template.
        Outside runtime: An EvalData placeholder that defers evaluation to the runtime.


    The decorated function or method must accept two positional arguments: (obj, tags), where:

    - obj is the instance (when used on methods) or an opaque object (when used on functions).
    - tags is a `TagWriter` used to declare Jinja2 flow control statements in a type-safe manner.
      Any variables recorded by this helper are provided as the template context when rendering.

    The function should return a string (typically built with an f-string).

    Example:

      .. code-block:: python

        class MyNode(tosca.nodes.Root):

          @jinja_template(convert_to="yaml")
          def _contents(self, tags: TagWriter) -> str:
              return f"""server:
                url: {self.url}
                { tags.if_(self.ports) }
                ports:
                  { tag.for_(
                      self.ports,
                      lambda port: f"- {tags.index}: {tags.expr("port")}"
                  }
                { tags.endif }
                """
          my_yaml_template: str = Computed(_contents)

      As this is interpreted as a Jinja2 template, the full Jinja2 syntax is available,
      however note that "{" and "}" need to be escaped in f-strings, so Jinja2 expression would look like ``f\"Hello {{{{ name }}}}\"``).
    '''
    from unfurl.eval import Ref
    from tosca import global_state_mode, global_state_context

    def _make_computed(func: _F) -> _F:
        def wrapped(obj):
            if global_state_mode() == "runtime":
                t = TagWriter()
                _template = func(obj, t)
                if convert_to:
                    _template = f"{{% filter from_{convert_to} %}}\n{_template}\n{{% endfilter %}}"
                expr = dict(eval={"template": _template})
                if t._vars:
                    expr["vars"] = t._vars
                # print(_template)
                # print(t._vars)
                return Ref(expr).resolve_one(global_state_context())
            else:
                kwargs = {"computed": [f"{func.__module__}:{func.__qualname__}:method"]}
                return EvalData({"eval": kwargs})

        # set to invoke wrapped() when generating yaml:
        setattr(wrapped, "_is_template_function", True)
        return cast(_F, wrapped)

    if _func is None:  # decorator()
        return _make_computed
    else:  # decorator
        return _make_computed(_func)


class TagWriter:
    """Declare Jinja2 flow control statements in a type-safe manner. See `jinja_template` for more."""

    def __init__(self):
        self._vars: Dict[str, Any] = {}
        self._exprs: Dict[int, str] = {}
        self._loops: List[List[Any]] = []
        self.index = -1

    def _cond(self, op: str, expr: Union[_T, EvalData]) -> _T:
        if not isinstance(expr, EvalData):
            expr = cast(_T, self._name_expr(expr))
        return cast(_T, f"{{% {op} {_strip_expr(expr)} %}}")

    def if_(self, expr: Union[_T, EvalData]) -> _T:
        """Returns {% if expr %} escaped."""
        return self._cond("if", expr)

    def elif_(self, expr: Union[_T, EvalData]) -> _T:
        """Returns {% elif expr } escaped."""
        return self._cond("elif", expr)

    @property
    def else_(self) -> str:
        """Returns {% else %} escaped."""
        return "{% else %}"

    @property
    def endif(self) -> str:
        """Returns {% endif %} escaped."""
        return "{% endif %}"

    def strip(self, expr: str) -> str:
        return _strip_expr(expr)

    def expr(self, expr: str) -> str:
        """Return ``expr`` as Jinja2 expression, i.e. ``{{ expr }}``.
        You need to call this for jinj2a expressions that reference the loop argument in a `for_` partial
        since they need to be rewritten.
        """
        if self._loops:
            arg_name, collection_name, index = self._loops[-1]
            expr = re.sub(
                rf"(^|\W){arg_name}(\W|$)", rf"\1{collection_name}[{index}]\2", expr
            )
        return "{{ " + expr + " }}"

    def for_(self, iterable: Iterable[_T], partial: Callable[[_T], str]) -> str:
        """
        Generate a string by iterating over an iterable and applying a partial function to each item.
        This method processes an iterable collection, applies the partial function to each item.

        Args:
          iterable (Iterable[_T]): The collection of items to iterate over.
          partial (Callable[[_T], str]): A function that takes a single item from the iterable and returns a string representation.
                                         The function's first parameter name will be used as the argument name in the loop context.
                                         You access the current position in the collection via the``tags.index`` property.

        Returns:
          str: A concatenated string of all results from applying the partial function to each item in the iterable.
        """
        self.arg_name = list(inspect.signature(partial).parameters)[0]
        collection_name = self._name_expr(iterable)
        self._loops.append([self.arg_name, collection_name, 0])
        src = ""
        items = []
        try:
            for index, item in enumerate(iterable):
                self._loops[-1][-1] = index
                items.append(item)
                self.index = index
                src += partial(item)
        finally:
            self._loops.pop()
        self._vars[collection_name] = items
        return src

    def _name_expr(self, expr) -> str:
        if id(expr) in self._exprs:
            return self._exprs[id(expr)]
        curr = "__l" + str(len(self._exprs))
        self._exprs[id(expr)] = curr
        self._vars[curr] = expr
        return curr

    def add_vars(self, **kwargs) -> Dict[str, Any]:
        self._vars.update(kwargs)
        return self._vars
