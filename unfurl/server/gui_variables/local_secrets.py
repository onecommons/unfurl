import os
from typing import Any, Dict, Iterator, List, Literal, Optional, Union, cast

from ...logs import is_sensitive, getLogger

from ...localenv import LocalEnv
from ruamel.yaml.comments import CommentedMap

from . import EnvVar

logger = getLogger("unfurl.gui")


def _get_env_vars(envs: Dict[str, dict], env_name: str) -> Dict[str, Any]:
    env = envs.get(env_name)
    if env:
        return env.get("variables", {})
    return {}


def _set_env_var(environments: Dict[str, dict], env_name: str, key: str, val: Any) -> Dict[str, Any]:
    env = environments.setdefault(env_name, CommentedMap())
    variables = env.setdefault("variables", CommentedMap())
    variables[key] = val
    return variables


def set_variables(localenv: LocalEnv, env_vars: List[EnvVar]) -> None:
    # reload immediately in case of user edits
    project = localenv.project or localenv.homeProject
    assert project
    project.reload()

    config = cast(Dict[str, Dict], project.localConfig.config.config)
    assert config
    envs = config.setdefault("environments", CommentedMap())

    secret_config_key, secret_config = project.localConfig.find_secret_include()
    if secret_config is not None:
        secret_environments = cast(
            Optional[Dict[str, Dict]],
            secret_config.setdefault("environments", CommentedMap()),
        )
    else:
        secret_environments = None

    modified_secrets = False
    modified_config = False
    for envvar in env_vars:
        environment_scope = envvar["environment_scope"]
        env_name = "defaults" if environment_scope == "*" else environment_scope
        config_env_vars = _get_env_vars(envs, env_name)
        if secret_environments is not None:
            secret_env_vars = _get_env_vars(secret_environments, env_name)
        else:
            secret_env_vars = {}
        key = envvar["key"]
        value = envvar.get("secret_value", envvar.get("value"))
        if envvar["variable_type"] == "file":
            value = {"eval": dict(tempfile=value)}
        if envvar.get("_destroy"):
            if key in config_env_vars:
                modified_config = True
                del config_env_vars[key]
            if key in secret_env_vars:
                modified_secrets = True
                del secret_env_vars[key]
        else:
            if envvar["masked"]:
                secret_val = {"eval": dict(sensitive=value)}  # mark sensitive
                if secret_environments:
                    _set_env_var(secret_environments, env_name, key, secret_val)
                    modified_secrets = True
                    if key in config_env_vars:
                        modified_config = True
                        del config_env_vars[key]
                else:
                    _set_env_var(envs, env_name, key, secret_val)
                    modified_config = True
            else:
                if key in secret_env_vars:  # in case masked flag changed
                    modified_secrets = True
                    del secret_env_vars[key]
                _set_env_var(envs, env_name, key, value)
                modified_config = True
    if modified_secrets:
        project.localConfig.config.save_include(secret_config_key)
    if modified_config:
        project.localConfig.config.save()
    if modified_secrets or modified_config:
        project.reload()


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
