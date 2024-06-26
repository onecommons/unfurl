import os
from typing import Any, Iterator, List, Literal, Optional, Union
import re
from . import EnvVar
from ...localenv import LocalEnv
from urllib.parse import ParseResult as URL
import gitlab
from urllib.parse import urlparse
from functools import lru_cache

UNFURL_SERVE_PATH = os.getenv("UNFURL_SERVE_PATH", "")


@lru_cache
def _get_context(localenv):
    localrepo = localenv.project.project_repoview.repo

    url = urlparse(localrepo.remote.url)

    [_, token, *_] = re.split(r"[@:]", url.netloc)
    origin = f"{url.scheme}://{url.hostname}"

    from rich import inspect

    inspect(token)
    inspect(origin)

    gl = gitlab.Gitlab(origin, private_token=token)
    gl.enable_debug()
    project = gl.projects.get(url.path[1:])

    return (gl, project)


def set_variables(localenv, env_vars: List[EnvVar]) -> LocalEnv:
    _, project = _get_context(localenv)

    for var in env_vars:
        data = {
            "key": var.get("key"),
            "value": var.get("secret_value") or var.get("value"),
            "environment_scope": var.get("environment_scope"),
            "masked": var.get("masked"),
            "variable_type": var.get("variable_type"),
            "protected": var.get("protected"),
        }

        if var.get("id"):
            project.variables.update(var["key"], data)
        else:
            project.variables.create(data)

    return localenv


def yield_variables(localenv) -> Iterator[EnvVar]:
    _, project = _get_context(localenv)

    for variable in project.variables.list():
        # the iteration here makes my language server happy for some reason
        v = EnvVar(**{key: value for key, value in variable.attributes.items()})
        v["secret_value"] = v.get("value")
        v["id"] = (v["environment_scope"] + ":" + str(v["key"]),)
        yield v
