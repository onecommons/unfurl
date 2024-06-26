from .envvar import EnvVar
from ...localenv import LocalEnv
from functools import lru_cache
from typing import Any, Iterator, List, Literal, Optional, Union

from . import local_secrets
from . import ufcloud_secrets


@lru_cache
def _get_secrets_manager(localenv):
    localrepo = localenv.project.project_repoview.repo
    localrepo_is_dashboard = bool(localenv.manifestPath)

    if localrepo_is_dashboard and localrepo.remote and localrepo.remote.url:
        return ufcloud_secrets
    else:
        return local_secrets


def set_variables(localenv, env_vars: List[EnvVar]) -> LocalEnv:
    return _get_secrets_manager(localenv).set_variables(localenv, env_vars)


def yield_variables(localenv) -> Iterator[EnvVar]:
    return _get_secrets_manager(localenv).yield_variables(localenv)
