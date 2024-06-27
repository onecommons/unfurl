import os
from typing import Iterator, List, Literal, Union

from ...logs import is_sensitive, getLogger

from ...localenv import LocalEnv
from ruamel.yaml.comments import CommentedMap

from . import EnvVar

UNFURL_SERVE_PATH = os.getenv("UNFURL_SERVE_PATH", "")


logger = getLogger("unfurl.gui")


def set_variables(localenv, env_vars: List[EnvVar]) -> LocalEnv:
    # reload immediately in case of user edits
    localenv = LocalEnv(UNFURL_SERVE_PATH, overrides={"ENVIRONMENT": "*"})
    project = localenv.project or localenv.homeProject
    assert project
    secret_config_key, secret_config = project.localConfig.find_secret_include()
    secret_environments = (
        secret_config.setdefault("environments", CommentedMap())
        if secret_config
        else None
    )
    config: dict = project.localConfig.config.config
    assert config
    env = config.setdefault("environments", CommentedMap())
    modified_secrets = False
    modified_config = False
    for envvar in env_vars:
        environment_scope = envvar["environment_scope"]
        env_name = "defaults" if environment_scope == "*" else environment_scope
        key = envvar["key"]
        value = envvar.get("secret_value", envvar.get("value"))
        if envvar["variable_type"] == "file":
            value = {"eval": dict(tempfile=value)}
        if envvar.get("_destroy"):
            if env_name in env and key in env[env_name]:
                modified_config = True
                del env[env_name][key]
            secret_env = (
                secret_environments.get(env_name) if secret_environments else None
            )
            if secret_env and key in secret_env.get("variables", {}):
                modified_secrets = True
                del secret_env["variables"][key]
        else:
            if envvar["masked"]:
                env.pop(key, None)  # in case this flag changed
                if secret_environments is not None:
                    secret_env = secret_environments.setdefault(
                        env_name, CommentedMap()
                    )
                    modified_secrets = True
                    secret_env.setdefault("variables", {})[key] = {
                        "eval": dict(sensitive=value)
                    }  # mark sensitive
            else:
                # in case this flag changed
                secret_env = (
                    secret_environments.get(env_name) if secret_environments else None
                )
                if secret_env and key in secret_env.get("variables", {}):
                    modified_secrets = True
                    del secret_env["variables"][key]
                modified_config = True
                env.setdefault(env_name, CommentedMap())[key] = value
    if modified_secrets:
        project.localConfig.config.save_include(secret_config_key)
    if modified_config:
        project.localConfig.config.save()
    if modified_secrets or modified_config:
        # reload
        localenv = LocalEnv(UNFURL_SERVE_PATH, overrides={"ENVIRONMENT": "*"})
    return localenv


def yield_variables(localenv) -> Iterator[EnvVar]:
    project = localenv.project or localenv.homeProject
    assert project
    for env_name, context in project.contexts.items():
        env_vars = context and context.get("variables")
        if not env_vars:
            continue
        if env_name == "defaults":
            scope = "*"
        else:
            scope = env_name
        for name, value in env_vars.items():
            masked = is_sensitive(value)
            variable_type: Union[Literal["env_var"], Literal["file"]] = "env_var"
            if isinstance(value, dict) and "eval" in value:
                eval_ref = value["eval"]
                if "sensitive" in eval_ref:
                    masked = True
                    value = eval_ref["sensitive"]
                if "tempfile" in eval_ref:
                    variable_type = "file"
                    value = eval_ref["tempfile"]
            yield EnvVar(
                id=scope + ":" + name,
                key=name,
                masked=masked,
                variable_type=variable_type,
                value=value,
                environment_scope=scope,
                protected=False,
                raw=False,
            )
