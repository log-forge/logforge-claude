import json
import sqlite3
from logforge_out import LogForge

connection = sqlite3.connect(":memory:")
connection.execute("create table orders (id integer primary key, quantity integer)")
connection.execute("insert into orders values (1, 2)")
connection.commit()
LogForge.out("CHECKOUT", lambda: {"order_id": 1, "quantity": 2}, tags=["database"], name="checkout.saved")
LogForge.check("checkout", "checkout.database-row", lambda: connection.execute("select quantity from orders where id=1").fetchone() == (2,), tags=["database"])
print("RESULT " + json.dumps({"order_id": 1, "status": "saved"}), flush=True)
connection.close()
