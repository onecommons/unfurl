# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
from configparser import ConfigParser
from xmlrpc.client import Fault, ServerProxy

from supervisor import xmlrpc

from ..configurator import Configurator


# support unix domain socket connections
# (we want to connect the same way as supervisorctl does for security
# and to automatically support multiple instances)
def get_server_proxy(serverurl=None, username=None, password=None):
    # copied from https://github.com/Supervisor/supervisor/blob/b52f49cff287c4d821c2c54d7d1afcd397b699e5/supervisor/options.py#L1718
    return ServerProxy(
        # dumbass ServerProxy won't allow us to pass in a non-HTTP url,
        # so we fake the url we pass into it and always use the transport's
        # 'serverurl' to figure out what to attach to
        "http://127.0.0.1",
        transport=xmlrpc.SupervisorTransport(username, password, serverurl),
    )


def _reload_config(server, name):
    result = server.supervisor.reloadConfig()
    return any((name in changed) for changed in result[0])


class SupervisorConfigurator(Configurator):
    def render(self, task):
        # host is a supervisord instance configured to load conf files in its "programs" directory
        # so write a .conf file there
        conf_dir = task.vars["HOST"]["homeDir"]
        name = task.vars["SELF"]["name"]
        conf_path = os.path.join(conf_dir, "programs", name + ".conf")

        op = task.configSpec.operation
        if op == "configure":
            program = task.vars["SELF"]["program"]
            program_dir = os.path.dirname(conf_path)
            task.logger.debug("writing %s", conf_path)
            if not os.path.isdir(program_dir):
                os.makedirs(program_dir)
            with open(conf_path, "w") as conff:
                conf = f"[program:{name}]\n"
                conf += "\n".join(f"{k}= {v}" for k, v in program.items())
                if "environment" not in program:
                    conf += "\nenvironment= "
                    conf += ",".join(
                        f'{k}="{v.replace("%", "%%")}"'
                        for (k, v) in task.get_environment(True).items()
                    )
                conff.write(conf)
        elif op == "delete":
            if os.path.exists(conf_path):
                os.remove(conf_path)

    def _get_config(self, task):
        host = task.vars["HOST"]
        conf_dir = os.path.abspath(host["homeDir"])
        conf = host["conf"]
        parser = ConfigParser(inline_comment_prefixes=(";", "#"), strict=False)
        parser.read_string(conf)
        server_config = dict(parser.items("supervisorctl", vars=dict(here=conf_dir)))
        server_config.pop("here", None)
        return server_config

    def run(self, task):
        name = task.vars["SELF"]["name"]

        # if homeDir is a relative path it will be relative to the baseDir of the host instance
        # which might be different from the current directory if host is an external instance
        server_config = self._get_config(task)
        server = get_server_proxy(**server_config)

        error = None
        op = task.configSpec.operation
        modified = False
        try:
            if op == "start":
                server.supervisor.startProcess(name)
                modified = True
            elif op == "stop":
                server.supervisor.stopProcess(name)
                modified = True
            elif op == "delete":
                # deleted in render()
                modified = _reload_config(server, name)
            elif op == "configure":
                # conf added/updated in render()
                modified = _reload_config(server, name)
                server.supervisor.addProcessGroup(name)
        except Fault as err:
            if (
                not (op == "start" and err.faultCode == 60)  # ok, 60 == ALREADY_STARTED
                and not (op == "stop" and err.faultCode == 70)  # ok, 70 == NOT_RUNNING
                and not (  # ok, 90 == ALREADY_ADDED
                    op == "configure" and err.faultCode == 90
                )
            ):
                error = "supervisor error: " + str(err)
            else:
                task.logger.debug("ignoring supervisord error: %s", str(err))

        yield task.done(success=not error, modified=modified, result=error)
