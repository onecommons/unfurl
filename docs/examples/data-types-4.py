class Auth(tosca.datatypes.Root):
  username: str = "admin"
  password: str

class Endpoint(tosca.datatypes.Root):
    ip: str
    port: int = 2233

class Connection(tosca.datatypes.Root):
    endpoint: Endpoint
    auth: Auth

class DatabaseService(tosca.nodes.DBMS):
    connection: Connection

my_db_service = DatabaseService(
    "my_db_service",
    connection=Connection(
        endpoint=Endpoint(
            ip="192.168.15.85"
        ),
        auth=Auth(
            password="secret"
        )
    )
)

