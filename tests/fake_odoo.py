"""Фейковий Odoo XML-RPC сервер для тестів (in-memory)."""

import threading
from xmlrpc.server import MultiPathXMLRPCServer, SimpleXMLRPCDispatcher, SimpleXMLRPCRequestHandler
from xmlrpc.client import Fault

DB, LOGIN, KEY, UID = "testdb", "bot@example.com", "secret-key", 7


class _Handler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")


class FakeOdoo:
    def __init__(self):
        self.records = {"res.partner": {}, "account.move": {}, "ir.attachment": {}, "res.users": {UID: {"id": UID, "name": "Бот"}}}
        self.messages = []
        self.next_id = 100
        self.calls = []
        self.server = MultiPathXMLRPCServer(("127.0.0.1", 0), requestHandler=_Handler, logRequests=False, allow_none=True)
        common = SimpleXMLRPCDispatcher(allow_none=True, encoding=None)
        common.register_function(lambda: {"server_version": "17.0-fake"}, "version")
        common.register_function(self.authenticate, "authenticate")
        obj = SimpleXMLRPCDispatcher(allow_none=True, encoding=None)
        obj.register_function(self.execute_kw, "execute_kw")
        self.server.add_dispatcher("/xmlrpc/2/common", common)
        self.server.add_dispatcher("/xmlrpc/2/object", obj)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def authenticate(self, db, login, key, env):
        return UID if (db, login, key) == (DB, LOGIN, KEY) else False

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _match(rec, domain):
        for field, op, value in domain:
            v = rec.get(field)
            if op == "=" and v != value:
                return False
            if op == "=ilike" and (str(v or "").casefold() != str(value).casefold()):
                return False
        return True

    def execute_kw(self, db, uid, key, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if (db, uid, key) != (DB, UID, KEY):
            raise Fault(1, "Access Denied")
        self.calls.append((model, method, args, kwargs))
        table = self.records.setdefault(model, {})
        if method == "search":
            ids = [i for i, r in table.items() if self._match(r, args[0])]
            return ids[: kwargs.get("limit") or None]
        if method == "search_count":
            return len([1 for r in table.values() if self._match(r, args[0])])
        if method == "create":
            vals = dict(args[0])
            if model == "res.partner" and "nonexistent_field" in vals:
                raise Fault(2, "Traceback...\nValueError: Invalid field 'nonexistent_field' on model 'res.partner'")
            new_id = self._new_id()
            if model == "account.move":
                vals.setdefault("state", "draft")
                vals["lines"] = [cmd[2] for cmd in vals.pop("invoice_line_ids", []) if cmd[0] == 0]
            vals["id"] = new_id
            table[new_id] = vals
            return new_id
        if method == "read":
            fields = kwargs.get("fields")
            out = []
            for i in args[0]:
                if i in table:
                    rec = table[i]
                    out.append({f: rec.get(f, False) for f in fields} | {"id": i} if fields else rec)
            return out
        if method == "write":
            for i in args[0]:
                vals = dict(args[1])
                cmds = vals.pop("invoice_line_ids", None)
                table[i].update(vals)
                if cmds is not None:
                    for cmd in cmds:
                        if cmd[0] == 5:
                            table[i]["lines"] = []
                        elif cmd[0] == 0:
                            table[i]["lines"].append(cmd[2])
            return True
        if method == "message_post":
            self.messages.append((args[0][0], kwargs.get("body")))
            return 1
        raise Fault(3, f"Unknown method {method}")
