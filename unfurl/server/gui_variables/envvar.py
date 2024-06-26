from typing import Any, Iterator, List, Literal, Optional, Union
from typing_extensions import TypedDict, Required


class EnvVar(TypedDict, total=False):
    # see https://docs.gitlab.com/ee/api/project_level_variables.html
    id: Union[int, str]  # ID or URL-encoded path of the project
    key: Required[str]
    masked: Required[bool]
    environment_scope: Required[str]
    value: Any
    secret_value: Any  # ??? not sent by api
    _destroy: bool
    variable_type: Required[Union[Literal["env_var"], Literal["file"]]]
    raw: Literal[False]  # if true value isn't expanded
    protected: Literal[False]
