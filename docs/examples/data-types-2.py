class Endpoint(tosca.datatypes.Root):
    ip: str
    port: int

class ExtendedEndpoint(Endpoint):
    username: str

class DatabaseService(tosca.nodes.DBMS):
    endpoint: ExtendedEndpoint

my_db_service = DatabaseService(
    "my_db_service",
    endpoint=ExtendedEndpoint(
        ip="192.168.15.85",
        port=2233,
        username="jimmy"
    )
)

__all__ = ["Endpoint", "ExtendedEndpoint", "DatabaseService", "my_db_service"]
