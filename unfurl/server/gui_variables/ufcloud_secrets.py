import os
from typing import Iterator, List
import re
from . import EnvVar
from ..serve import app
from ...localenv import LocalEnv
import urllib.parse
import gitlab
from functools import lru_cache

UNFURL_SERVE_PATH = os.getenv("UNFURL_SERVE_PATH", "")


@lru_cache
def _get_context(localenv):
    url, _ = localenv.project.localConfig.config.search_includes(
        pathPrefix=app.config["UNFURL_CLOUD_SERVER"]
    )

    parsed_url = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    private_token = query_params.get("private_token", [None])[0]
    project_id_match = re.search(
        r"projects/(?P<project_id>(\w|%2F|[/\-_])+)/variables", parsed_url.path
    )

    if project_id_match and private_token:
        project_id = project_id_match.group("project_id").replace("%2F", "/")
        origin = f"{parsed_url.scheme}://{parsed_url.hostname}"

        gl = gitlab.Gitlab(origin, private_token=private_token)
        gl.auth()
        gl.enable_debug()
        project = gl.projects.get(project_id)

        return (gl, project)
    else:
        raise ValueError(
            f"Could not access project_id and/or private_token from url '{url}'"
        )


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
