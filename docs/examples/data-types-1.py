class Endpoint(tosca.datatypes.Root):
    ip: str
    port: int

class DatabaseService(tosca.nodes.DBMS):
    endpoint: Endpoint

my_db_service = DatabaseService(
    "my_db_service",
    endpoint=Endpoint(
        ip="192.168.15.85",
        port=2233
    )
)

__all__ = ["Endpoint", "DatabaseService"]
