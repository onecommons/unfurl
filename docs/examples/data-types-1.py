import unfurl
import tosca
from tosca import DataEntity


class my_datatypes_Endpoint(DataEntity):
    """Socket endpoint details"""

    _type_name = "my.datatypes.Endpoint"
    ip: str
    """the endpoint IP"""

    port: int
    """the endpoint port"""


class DatabaseService(tosca.nodes.DBMS):
    endpoint: "my_datatypes_Endpoint"


my_db_service = DatabaseService(
    "my_db_service",
    endpoint=my_datatypes_Endpoint(
        ip="192.168.15.85",
        port=2233,
    ),
)

