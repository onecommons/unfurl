import re
from functools import lru_cache
from typing import Iterator, List

from .envvar import EnvVar
from ...localenv import LocalEnv
from ..serve import app

from . import local_secrets
from . import ufcloud_secrets


@lru_cache
def _get_secrets_manager(localenv):
    url, _ = localenv.project.localConfig.config.search_includes(
        pathPrefix=app.config["UNFURL_CLOUD_SERVER"]
    )

    gitlab_api_match = re.search(
        r"/api/v4/projects/(?P<project_id>(\w|%2F|[/\-_])+)/variables\?[^&]*&private_token=(?P<private_token>[^&]+)",
        str(url),
    )

    if gitlab_api_match:
        return ufcloud_secrets
    else:
        return local_secrets


def set_variables(localenv, env_vars: List[EnvVar]) -> LocalEnv:
    return _get_secrets_manager(localenv).set_variables(localenv, env_vars)


def yield_variables(localenv) -> Iterator[EnvVar]:
    return _get_secrets_manager(localenv).yield_variables(localenv)
