import re
from functools import lru_cache
from typing import Any, Iterator, List, Literal, Union
from typing_extensions import TypedDict, Required

from ...localenv import LocalEnv
from ..serve import app

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


@lru_cache
def _get_secrets_manager(localenv: LocalEnv):
    # avoid circular dependencies
    from . import local_secrets
    from . import ufcloud_secrets

    url, project_id = ufcloud_secrets.find_gitlab_endpoint(localenv)
    if project_id:
        return ufcloud_secrets
    return local_secrets


def set_variables(localenv: LocalEnv, env_vars: List[EnvVar]):
    return _get_secrets_manager(localenv).set_variables(localenv, env_vars)


def yield_variables(localenv: LocalEnv) -> Iterator[EnvVar]:
    return _get_secrets_manager(localenv).yield_variables(localenv)
