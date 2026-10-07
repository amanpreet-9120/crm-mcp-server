"""In-memory stand-in for the HubSpot CRM API endpoints the hubspot backend uses.

It replaces hubspot._request, so tests exercise the real request bodies, paging,
association reads and search filters without a HubSpot account.
"""

from __future__ import annotations

from datetime import datetime, timezone

# HUBSPOT_DEFINED association type -> (from type, to type)
ASSOC_TYPES = {1: ("contacts", "companies"), 5: ("deals", "companies"), 3: ("deals", "contacts"),
               206: ("calls", "deals"), 210: ("emails", "deals"), 212: ("meetings", "deals"), 214: ("notes", "deals")}

PIPELINE = {"id": "default", "label": "Sales Pipeline", "stages": [
    {"id": "appointmentscheduled", "label": "Appointment Scheduled", "metadata": {"probability": "0.2", "isClosed": "false"}},
    {"id": "qualifiedtobuy", "label": "Qualified To Buy", "metadata": {"probability": "0.4", "isClosed": "false"}},
    {"id": "presentationscheduled", "label": "Presentation Scheduled", "metadata": {"probability": "0.6", "isClosed": "false"}},
    {"id": "decisionmakerboughtin", "label": "Decision Maker Bought-In", "metadata": {"probability": "0.8", "isClosed": "false"}},
    {"id": "contractsent", "label": "Contract Sent", "metadata": {"probability": "0.9", "isClosed": "false"}},
    {"id": "closedwon", "label": "Closed Won", "metadata": {"probability": "1.0", "isClosed": "true"}},
    {"id": "closedlost", "label": "Closed Lost", "metadata": {"probability": "0.0", "isClosed": "true"}},
]}


def _num(value: str) -> float:
    """Comparable number for a stored value: dates become epoch ms, like HubSpot."""
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")) if "T" in s else datetime.fromisoformat(s + "T00:00:00+00:00")
        return dt.astimezone(timezone.utc).timestamp() * 1000
    return float(s)


class FakeHubSpot:
    def __init__(self):
        self.objects: dict[str, dict[int, dict]] = {}
        self.links: dict[tuple[str, str], dict[int, list[int]]] = {}
        self.properties: dict[tuple[str, str], dict] = {("companies", "industry"): {"name": "industry", "options": []}}
        self.next_id: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ storage

    def create(self, obj_type: str, props: dict, associations: list | None = None) -> dict:
        self.next_id[obj_type] = self.next_id.get(obj_type, 0) + 1
        oid = self.next_id[obj_type]
        stored = {k: str(v) for k, v in props.items() if v not in (None, "")}
        stored.setdefault("createdate", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.objects.setdefault(obj_type, {})[oid] = stored
        for a in associations or []:
            frm, to = ASSOC_TYPES[a["types"][0]["associationTypeId"]]
            assert frm == obj_type, f"association type {a['types'][0]} is not from {obj_type}"
            self.link(frm, oid, to, int(a["to"]["id"]))
        return {"id": str(oid), "properties": dict(stored)}

    def link(self, frm: str, from_id: int, to: str, to_id: int) -> None:
        self.links.setdefault((frm, to), {}).setdefault(from_id, []).append(to_id)
        self.links.setdefault((to, frm), {}).setdefault(to_id, []).append(from_id)

    def _obj(self, obj_type: str, oid: int) -> dict:
        return {"id": str(oid), "properties": dict(self.objects[obj_type][oid])}

    def _matches(self, props: dict, f: dict) -> bool:
        value, op = props.get(f["propertyName"]), f["operator"]
        if op == "HAS_PROPERTY":
            return value is not None
        if op == "NOT_HAS_PROPERTY":
            return value is None
        if op == "EQ":
            return value == f["value"]
        if op == "IN":
            return value in f["values"]
        if op == "NOT_IN":
            return value not in f["values"]
        if value is None:
            return False
        v = _num(value)
        if op == "GTE":
            return v >= float(f["value"])
        if op == "LTE":
            return v <= float(f["value"])
        if op == "BETWEEN":
            return float(f["value"]) <= v <= float(f["highValue"])
        raise AssertionError(f"operator {op} not supported by fake")

    # ------------------------------------------------------------------ HTTP

    def __call__(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        if path == "/crm/v3/pipelines/deals":
            return {"results": [PIPELINE]}
        if path == "/crm/v3/owners":
            return {"results": []}
        if parts[:3] == ["crm", "v3", "properties"]:
            obj_type = parts[3]
            if method == "GET":
                return self.properties.get((obj_type, parts[4]))
            assert body["name"] and body["type"] and body["fieldType"] and body["groupName"]
            self.properties[(obj_type, body["name"])] = body
            return body
        if parts[:3] == ["crm", "v4", "associations"]:
            frm, to = parts[3], parts[4]
            assert parts[5:] == ["batch", "read"]
            table = self.links.get((frm, to), {})
            return {"status": "COMPLETE", "results": [
                {"from": {"id": i["id"]}, "to": [{"toObjectId": t, "associationTypes": []} for t in table[int(i["id"])]]}
                for i in body["inputs"] if table.get(int(i["id"]))]}
        assert parts[:3] == ["crm", "v3", "objects"], path
        obj_type, rest = parts[3], parts[4:]
        store = self.objects.setdefault(obj_type, {})
        if rest == ["batch", "create"]:
            assert len(body["inputs"]) <= 100
            return {"status": "COMPLETE", "results": [self.create(obj_type, i["properties"], i.get("associations"))
                                                      for i in body["inputs"]]}
        if rest == ["batch", "read"]:
            assert len(body["inputs"]) <= 100
            return {"status": "COMPLETE", "results": [self._obj(obj_type, int(i["id"])) for i in body["inputs"]
                                                      if int(i["id"]) in store]}
        if rest == ["batch", "archive"]:
            for i in body["inputs"]:
                store.pop(int(i["id"]), None)
            return None
        if rest == ["search"]:
            assert len(body.get("filterGroups", [])) <= 5 and body.get("limit", 10) <= 200
            groups = body.get("filterGroups") or [{"filters": []}]
            hits = [oid for oid in sorted(store)
                    if any(all(self._matches(store[oid], f) for f in g["filters"]) for g in groups)]
            start = int(body.get("after") or 0)
            page = hits[start:start + body.get("limit", 10)]
            out = {"total": len(hits), "results": [self._obj(obj_type, oid) for oid in page]}
            if start + len(page) < len(hits):
                out["paging"] = {"next": {"after": str(start + len(page))}}
            return out
        if not rest and method == "POST":
            return self.create(obj_type, body["properties"], body.get("associations"))
        if not rest and method == "GET":
            ids = sorted(store)
            start, limit = int((params or {}).get("after") or 0), int((params or {}).get("limit") or 10)
            out = {"results": [self._obj(obj_type, oid) for oid in ids[start:start + limit]]}
            if start + limit < len(ids):
                out["paging"] = {"next": {"after": str(start + limit)}}
            return out
        oid = int(rest[0])
        if oid not in store:
            return None
        if method == "GET":
            return self._obj(obj_type, oid)
        if method == "PATCH":
            for k, v in body["properties"].items():
                if v in (None, ""):
                    store[oid].pop(k, None)
                else:
                    store[oid][k] = str(v)
            return self._obj(obj_type, oid)
        raise AssertionError(f"{method} {path} not supported by fake")
