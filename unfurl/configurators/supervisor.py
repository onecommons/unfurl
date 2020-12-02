# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os.path

import six
from six.moves.configparser import ConfigParser

from supervisor import xmlrpc

try:
    from xmlrpc.client import ServerProxy, Fault
except ImportError:
    from xmlrpclib import Server as ServerProxy
    from xmlrpclib import Fault

from ..configurator import Configurator
from ..support import abspath

# support unix domain socket connections
# (we want to connect the same way as supervisorctl does for security and to automatically support multiple instances)
def getServerProxy(serverurl=None, username=None, password=None):
    # copied from https://github.com/Supervisor/supervisor/blob/b52f49cff287c4d821c2c54d7d1afcd397b699e5/supervisor/options.py#L1718
    return ServerProxy(
        # dumbass ServerProxy won't allow us to pass in a non-HTTP url,
        # so we fake the url we pass into it and always use the transport's
        # 'serverurl' to figure out what to attach to
        "http://127.0.0.1",
        transport=xmlrpc.SupervisorTransport(username, password, serverurl),
    )


def _reloadConfig(server, name):
    result = server.supervisor.reloadConfig()
    return any((name in changed) for changed in result[0])


class SupervisorConfigurator(Configurator):
    def run(self, task):
        host = task.vars["HOST"]

        # if homeDir is a relative path it will be relative to the baseDir of the host instance
        # which might be different from the current directory if host is an external instance
        confDir = abspath(host.context, host["homeDir"])
        conf = host["conf"]

        name = task.vars["SELF"]["name"]
        confPath = os.path.join(confDir, "programs", name + ".conf")
        if six.PY3:
            parser = ConfigParser(inline_comment_prefixes=(";", "#"), strict=False)
            parser.read_string(conf)
        else:
            parser = ConfigParser()
            parser.readfp(six.StringIO(conf))
        serverConfig = dict(parser.items("supervisorctl", vars=dict(here=confDir)))
        serverConfig.pop("here", None)
        server = getServerProxy(**serverConfig)

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
                if os.path.exists(confPath):
                    os.remove(confPath)
                modified = _reloadConfig(server, name)
            elif op == "configure":
                program = task.vars["SELF"]["program"]
                programDir = os.path.dirname(confPath)
                task.logger.debug("writing %s", confPath)
                if not os.path.isdir(programDir):
                    os.makedirs(programDir)
                with open(confPath, "w") as conff:
                    conf = "[program:%s]\n" % name
                    conf += "\n".join("%s= %s" % (k, v) for k, v in program.items())
                    if "environment" not in program:
                        conf += "\nenvironment= "
                        conf += ",".join(
                            '%s="%s"' % (k, v.replace("%", "%%"))
                            for (k, v) in task.getEnvironment(True).items()
                        )
                    conff.write(conf)
                modified = _reloadConfig(server, name)
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

        yield task.done(success=not error, modified=modified, result=error)
