class MongoDatabaseExtended(tosca.nodes.DBMS):
  enable_replication: bool = False

  def create(self, **kw):
      return unfurl.configurators.shell.ShellConfigurator(
        command=["scripts/mongo/install-mongo-extended.sh"]
      )

  def configure(self, **kw):
      return unfurl.congifurators.shell.ShellConfigurator(
        command=["scripts/mongo/configure-mongo-extended.sh"]
      )

mongo_database_extended = MongoDatabaseExtended("mongo_database_extended")

__all__ = ["MongoDatabaseExtended", "mongo_database_extended"]

