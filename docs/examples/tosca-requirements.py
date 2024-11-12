import tosca
from tosca import Eval, Node, Property, Relationship

class DatabaseConnection(tosca.relationships.ConnectsTo):
    username: str
    password: str = Property(metadata={"sensitive": True})


class MyApplication(tosca.nodes.SoftwareComponent):
    db: "DatabaseConnection | tosca.nodes.DBMS"


mydb_connection = DatabaseConnection(
    "mydb_connection",
    username="myapp",
    password=Eval({"eval": {"secret": "myapp_db_pw"}}),
)
myApp = MyApplication(
    "myApp",
    db=mydb_connection,
)
