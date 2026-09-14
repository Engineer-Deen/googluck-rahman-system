import json
import urllib.request

import websocket

targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
print("targets", targets)
ws = websocket.create_connection(targets[0]["webSocketDebuggerUrl"], timeout=10)


def send(method, params=None, msg_id=1):
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    while True:
        data = json.loads(ws.recv())
        if data.get("id") == msg_id:
            return data


print("enable", send("Runtime.enable"))
print("eval", send("Runtime.evaluate", {"expression": "1+1", "returnByValue": True}, 2))
print("url", send("Runtime.evaluate", {"expression": "location.href", "returnByValue": True}, 3))
print(
    "hasLogin",
    send(
        "Runtime.evaluate",
        {"expression": "!!document.getElementById('login-overlay')", "returnByValue": True},
        4,
    ),
)
ws.close()
