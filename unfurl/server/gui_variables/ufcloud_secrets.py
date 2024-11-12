import os
from typing import Iterator, List, Optional, Tuple
import re
from . import EnvVar
from ..serve import app
from ...localenv import LocalEnv
import urllib.parse
import gitlab
from functools import lru_cache


def find_gitlab_endpoint(localenv: LocalEnv) -> Tuple[Optional[str], Optional[str]]:
    if not localenv.project:
        return None, None
    url, _ = localenv.project.localConfig.config.search_includes(
        pathPrefix=app.config["UNFURL_CLOUD_SERVER"]
    )
    if not url:
        return url, None
    project_id_match = re.search(
        r"/api/v4/projects/(?P<project_id>(\w|%2F|[/\-_])+)/variables", url
    )
    if not project_id_match:
        return url, None
    project_id = project_id_match.group("project_id").replace("%2F", "/")
    return url, project_id


@lru_cache
def _get_context(localenv: LocalEnv):
    url, project_id = find_gitlab_endpoint(localenv)
    if not url or not project_id:
        return None

    parsed_url = urllib.parse.urlparse(url)
    if not parsed_url.query:
        return None

    query_params = urllib.parse.parse_qs(str(parsed_url.query))
    private_token = query_params.get("private_token", [""])[0]
    if not private_token:
        return None

    origin = f"{parsed_url.scheme}://{parsed_url.hostname}"

    gl = gitlab.Gitlab(origin, private_token=private_token)
    gl.auth()
    gl.enable_debug()
    project = gl.projects.get(project_id)

    return project


def set_variables(localenv: LocalEnv, env_vars: List[EnvVar]) -> None:
    project = _get_context(localenv)
    if not project:
        raise ValueError("Could not access project_id and/or private_token from url")

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


def yield_variables(localenv: LocalEnv) -> Iterator[EnvVar]:
    project = _get_context(localenv)
    if not project:
        raise ValueError("Could not access project_id and/or private_token from url")

    for variable in project.variables.list(get_all=True):
        yield EnvVar(
            **{
                **variable.attributes,
                "id": variable.attributes["key"]
                + ":"
                + variable.attributes["environment_scope"],
                "secret_value": variable.attributes["value"],
                "key": variable.attributes["key"],
                "masked": variable.attributes["masked"],
                "environment_scope": variable.attributes["environment_scope"],
                "variable_type": variable.attributes["variable_type"],
            }
        )
