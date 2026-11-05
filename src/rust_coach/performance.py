import asyncio
import hashlib
import inspect
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import perf_counter
from uuid import uuid4


logger = logging.getLogger("rust_coach")
_operation = ContextVar("performance_operation", default=None)
_span = ContextVar("performance_span", default=None)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def error_code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code) else type(error).__name__


def normalize_usage(raw):
    result = {"status": "missing", "input_tokens": None, "output_tokens": None,
              "cached_input_tokens": None, "reasoning_tokens": None, "total_tokens": None}
    if not isinstance(raw, dict) or not raw:
        return result
    invalid = False

    def number(*values):
        nonlocal invalid
        present = [value for value in values if value is not None]
        if not present:
            return None
        if any(type(value) is not int or value < 0 for value in present) or len(set(present)) != 1:
            invalid = True
            return None
        return present[0]

    result["input_tokens"] = number(raw.get("input_tokens"), raw.get("prompt_tokens"))
    result["output_tokens"] = number(raw.get("output_tokens"), raw.get("completion_tokens"))
    result["total_tokens"] = number(raw.get("total_tokens"))
    for target, details in (("cached_input_tokens", ("input_tokens_details", "prompt_tokens_details", "cached_tokens")),
                            ("reasoning_tokens", ("output_tokens_details", "completion_tokens_details", "reasoning_tokens"))):
        entries = [raw.get(name) for name in details[:2] if raw.get(name) is not None]
        if any(not isinstance(entry, dict) for entry in entries):
            invalid = True
        else:
            result[target] = number(*(entry.get(details[2]) for entry in entries))
    input_tokens, output_tokens = result["input_tokens"], result["output_tokens"]
    if input_tokens is not None and output_tokens is not None and result["total_tokens"] is not None:
        invalid |= input_tokens + output_tokens != result["total_tokens"]
    for subset, total in (("cached_input_tokens", input_tokens), ("reasoning_tokens", output_tokens)):
        if result[subset] is not None and total is not None:
            invalid |= result[subset] > total
    result["status"] = "invalid" if invalid else "complete" if input_tokens is not None and output_tokens is not None else "partial"
    if invalid:
        result.update({key: None for key in result if key != "status"})
    return result


def summarize(spans, *, incomplete=False):
    calls = [item for item in spans if item["kind"] == "model"]
    web = [item for item in spans if item["kind"] == "web"]
    tokens = {}
    for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"):
        values = [item.get("usage", {}).get(field) for item in calls]
        known = [value for value in values if value is not None]
        tokens[field] = {"known_sum": sum(known) if known else None,
                         "reported_calls": len(known), "total_calls": len(calls)}
    return {"model_calls": len(calls), "web_requests": len(web),
            "dispatched_model_requests": sum(item.get("dispatched") is True for item in calls),
            "injected_model_calls": sum(item.get("adapter") == "injected" for item in calls),
            "failed_calls": sum(item["status"] == "failed" for item in calls + web),
            "cancelled_calls": sum(item["status"] == "cancelled" for item in calls + web),
            "unfinished_calls": sum(item["status"] == "running" for item in calls + web),
            "usage_complete_calls": sum(item.get("usage", {}).get("status") == "complete" for item in calls),
            "tokens": tokens, "billed_cost": None,
            "cost_status": "unknown" if calls or web or incomplete else "no_external_calls",
            "cost_note": "无供应商价格和账单，不将调用数或缺失usage换算为零费用。",
            "recording_status": "incomplete" if incomplete else "complete"}


class PerformanceStore:
    def __init__(self, conversations):
        self.conversations = conversations
        self.write_failed = False
        root = Path(__file__).parent
        sources = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(root.rglob("*.py"))}
        self.source_sha256 = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
        with conversations.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS performance_runs ("
                "id TEXT PRIMARY KEY, conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,"
                "request_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS performance_request ON performance_runs(conversation_id,request_id);"
                "CREATE TABLE IF NOT EXISTS performance_spans ("
                "id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES performance_runs(id) ON DELETE CASCADE,"
                "payload TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS performance_span_run ON performance_spans(run_id);"
            )

    def write_run(self, data):
        return self.write("INSERT INTO performance_runs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                          (data["id"], data["conversation_id"], data["request_id"], data["kind"], json.dumps(data)))

    def write_span(self, run_id, data):
        return self.write("INSERT INTO performance_spans VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                          (data["id"], run_id, json.dumps(data)))

    def write(self, statement, parameters):
        try:
            with self.conversations.connection() as connection:
                connection.execute(statement, parameters)
            return True
        except sqlite3.Error:
            if not self.write_failed:
                logger.error("performance_recording_failed; metrics incomplete")
            self.write_failed = True
            return False

    @contextmanager
    def run(self, kind, *, conversation_id=None, request_id=None, parent_run_id=None):
        if conversation_id is not None:
            with self.conversations.connection() as connection:
                if not connection.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
                    conversation_id = None
        operation = Operation(self, kind, conversation_id, request_id, parent_run_id)
        token = _operation.set(operation)
        parent = _span.set(None)
        try:
            yield operation
        except BaseException as error:
            operation.data.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                                  error_code=error_code(error))
            raise
        finally:
            operation.finish()
            _span.reset(parent)
            _operation.reset(token)

    def inspect(self, *, conversation_id=None, request_id=None, limit=100):
        clauses, parameters = [], []
        if conversation_id is not None:
            self.conversations.get(conversation_id)
            clauses.append("conversation_id=?")
            parameters.append(conversation_id)
        if request_id is not None:
            clauses.append("request_id=?")
            parameters.append(request_id)
        query = "SELECT payload FROM performance_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        runs = []
        with self.conversations.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM (" + query + ")", parameters).fetchone()[0]
            for row in connection.execute(query + " ORDER BY rowid DESC LIMIT ?", [*parameters, limit]):
                run = json.loads(row["payload"])
                run["spans"] = [json.loads(span["payload"]) for span in connection.execute(
                    "SELECT payload FROM performance_spans WHERE run_id=? ORDER BY rowid", (run["id"],))]
                run["summary"] = summarize(run["spans"], incomplete=self.write_failed or run.get("recording_incomplete", False))
                runs.append(run)
            background = None
            if conversation_id is not None and request_id is not None:
                row = connection.execute("SELECT memory FROM turns WHERE conversation_id=? AND request_id=?",
                                         (conversation_id, request_id)).fetchone()
                if row and row["memory"]:
                    background = json.loads(row["memory"]).get("extraction", {}).get("status")
        return {"version": "performance-v1", "runs": list(reversed(runs)), "total_runs": count,
                "truncated": count > limit, "background_memory_status": background,
                "summary": summarize([span for run in runs for span in run["spans"]],
                                     incomplete=self.write_failed or any(run.get("recording_incomplete") for run in runs)),
                "scope": "返回的运行记录；前台、后台、探测分别统计；非浏览器端到端时间。",
                "shared_request_budget": False}


class Operation:
    def __init__(self, store, kind, conversation_id, request_id, parent_run_id):
        self.store = store
        self.started = perf_counter()
        self.closed = False
        self.data = {"id": str(uuid4()), "kind": kind, "conversation_id": conversation_id,
                     "request_id": request_id, "parent_run_id": parent_run_id, "started_at": timestamp(),
                     "ended_at": None, "duration_ms": None, "status": "running", "error_code": None,
                     "progress": [], "observations": [], "recording_incomplete": False,
                     "source_sha256": store.source_sha256, "measurement_version": "performance-v1"}
        self.save()

    def save(self):
        if not self.store.write_run(self.data):
            self.data["recording_incomplete"] = True

    def finish(self):
        self.closed = True
        self.data.update(ended_at=timestamp(), duration_ms=round((perf_counter() - self.started) * 1000, 3))
        if self.data["status"] == "running":
            self.data["status"] = "completed"
        self.save()


def current_operation():
    operation = _operation.get()
    return operation if operation and not operation.closed else None


@contextmanager
def span(name, kind="stage", **metadata):
    operation = current_operation()
    if operation is None:
        yield {}
        return
    started = perf_counter()
    parent = _span.get()
    item = {"id": str(uuid4()), "parent_span_id": parent.get("id") if parent else None,
            "name": name, "kind": kind, "started_at": timestamp(), "ended_at": None,
            "start_offset_ms": round((started - operation.started) * 1000, 3),
            "duration_ms": None, "status": "running", **metadata}
    if not operation.store.write_span(operation.data["id"], item):
        operation.data["recording_incomplete"] = True
    token = _span.set(item)
    try:
        yield item
    except BaseException as error:
        item.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed", error_code=error_code(error))
        raise
    finally:
        item.update(ended_at=timestamp(), duration_ms=round((perf_counter() - started) * 1000, 3))
        if item["status"] == "running":
            item["status"] = "completed"
        if not operation.store.write_span(operation.data["id"], item):
            operation.data["recording_incomplete"] = True
        _span.reset(token)


def measured(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with span(name):
                    return await function(*args, **kwargs)
            return asynchronous
        @wraps(function)
        def synchronous(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return synchronous
    return decorate


def observe(name, **values):
    operation = current_operation()
    if operation:
        operation.data["observations"].append({"name": name, **values})


def progress(stage):
    operation = current_operation()
    if operation and (not operation.data["progress"] or operation.data["progress"][-1]["stage"] != stage):
        operation.data["progress"].append({"stage": stage, "offset_ms": round((perf_counter() - operation.started) * 1000, 3)})


def provider_dispatched():
    item = _span.get()
    if item and item.get("kind") == "model":
        item["dispatched"] = True
        operation = current_operation()
        if operation and not operation.store.write_span(operation.data["id"], item):
            operation.data["recording_incomplete"] = True


def provider_usage(raw):
    item = _span.get()
    if item and item.get("kind") == "model":
        item["usage"] = normalize_usage(raw)


class MeteredModel:
    def __init__(self, wrapped, store, role, settings):
        self.wrapped, self.store, self.role, self.settings = wrapped, store, role, settings

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    async def complete(self, messages):
        if current_operation() is None:
            with self.store.run("standalone_model"):
                return await self.complete(messages)
        text_chars = 0
        images = 0
        for message in messages:
            if isinstance(message.content, str):
                text_chars += len(message.content)
            else:
                text_chars += sum(len(getattr(part, "text", "")) for part in message.content)
                images += sum(getattr(part, "type", None) == "image" for part in message.content)
        with span(self.role, "model", model=self.settings.model, api_mode=self.settings.api_mode,
                  endpoint_sha256=hashlib.sha256(self.settings.base_url.encode()).hexdigest(),
                  adapter="provider" if getattr(self.wrapped, "records_transport", False) else "injected",
                  dispatched=False, input_text_chars=text_chars, image_count=images,
                  max_output_tokens=self.settings.max_output_tokens, usage=normalize_usage(None)) as item:
            reply = await self.wrapped.complete(messages)
            item.update(usage=normalize_usage(reply.usage), output_chars=len(reply.content), truncated=reply.truncated)
            return reply


def metered_model(model, store, role, settings):
    if model is None:
        return model
    if isinstance(model, MeteredModel):
        if model.role == role and model.store is store:
            return model
        model = model.wrapped
    return MeteredModel(model, store, role, getattr(model, "settings", settings))
