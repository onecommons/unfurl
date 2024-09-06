class Auth(tosca.datatypes.Root):
    username: str
    password: str

class Endpoint(tosca.datatypes.Root):
    ip: str
    port: int

class Connection(tosca.datatypes.Root):
    endpoint: Endpoint
    auth: Auth

class DatabaseService(tosca.nodes.DBMS):
    connection: Connection

my_db_service = DatabaseService(
    "my_db_service",
    connection=Connection(
        endpoint=Endpoint(
            ip="192.168.15.85",
            port=2233
        ),
        auth=Auth(
            username="jimmy",
            password="secret"
        )
    )
)

__all__ = ["Auth", "Endpoint", "Connection", "DatabaseService", "my_db_service"]

