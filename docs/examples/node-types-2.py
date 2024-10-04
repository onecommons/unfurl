class MongoDatabaseExtended(MongoDatabase):
  enable_replication: bool = False
  "MongoDB replication enabling flag"

  def create(self, **kw):
      return unfurl.configurators.shell.ShellConfigurator(
        command=["scripts/mongo/install-mongo-extended.sh"]
      )

  def configure(self, **kw):
      return unfurl.congifurators.shell.ShellConfigurator(
        command=["scripts/mongo/configure-mongo-extended.sh"]
      )

