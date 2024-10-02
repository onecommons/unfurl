class MongoDatabase(tosca.nodes.DBMS):
  port: int
  "MongoDB port"

  def create(self, **kw):
      return unfurl.configurators.shell.ShellConfigurator(
        command=["scripts/mongo/install-mongo.sh"]
      )

  def start(self, **kw):
      return unfurl.configurators.shell.ShellConfigurator(
        command=["scripts/mongo/start-mongo.sh"]
      )

  def stop(self, **kw):
      return unfurl.configurators.shell.ShellConfigurator(
        command=["scripts/mongo/stop-mongo.sh"]
      )

