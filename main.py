"""Basic MCP mcp-agent app integration with OpenAI Apps SDK.

The server exposes widget-backed tools that render the UI bundle within the
client directory. Each handler returns the HTML shell via an MCP resource and
returns structured content so the ChatGPT client can hydrate the widget."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
import mcp.types as types
from mcp.server.fastmcp import FastMCP
import uvicorn
from mcp_agent.app import MCPApp
import httpx

from mcp_agent.server.app_server import create_mcp_server_for_app


@dataclass(frozen=True)
class TodoWidget:
    identifier: str
    title: str
    template_uri: str
    invoking: str
    invoked: str
    html: str


@dataclass
class TodoTask:
    id: str
    title: str
    created_at: str = field(
        default_factory=lambda: datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    )
    done: bool = False


class TodoStore:
    """Task store persisted to disk with basic completion stats."""

    _STATE_PATH = Path(__file__).parent / "todo_state.json"

    def __init__(self) -> None:
        self._tasks: "OrderedDict[str, TodoTask]" = OrderedDict()
        self._completed_total: int = 0
        self._completed_log: List[Dict[str, Any]] = []
        self._last_completed_at: Optional[str] = None
        self._lock = asyncio.Lock()
        self._load_state()

    def _initial_tasks(self) -> List[TodoTask]:
        return [
            TodoTask(id="welcome", title="Welcome to your orange todo list"),
            TodoTask(id="explore", title="Add a new task with the add task tool"),
            TodoTask(id="clean-up", title="Remove a task using the remove task tool"),
        ]

    def _load_state(self) -> None:
        if self._STATE_PATH.exists():
            try:
                raw = json.loads(self._STATE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        else:
            raw = {}

        raw_tasks = raw.get("tasks")
        if isinstance(raw_tasks, list):
            tasks: List[TodoTask] = []
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                try:
                    tasks.append(TodoTask(**item))
                except TypeError:
                    continue
            if tasks:
                self._tasks = OrderedDict((task.id, task) for task in tasks)
        if not self._tasks:
            default_tasks = self._initial_tasks()
            self._tasks = OrderedDict((task.id, task) for task in default_tasks)

        self._completed_total = int(raw.get("completedCount", 0) or 0)
        raw_completed = raw.get("completedTasks")
        if isinstance(raw_completed, list):
            self._completed_log = [
                item
                for item in raw_completed
                if isinstance(item, dict)
                and "title" in item
                and "completed_at" in item
            ][-50:]
        last_completed = raw.get("lastCompletedAt")
        self._last_completed_at = str(last_completed) if last_completed else None

        if not self._STATE_PATH.exists():
            self._persist_sync()

    def _serialize_state(self) -> Dict[str, Any]:
        return {
            "tasks": [asdict(task) for task in self._tasks.values()],
            "completedCount": self._completed_total,
            "lastCompletedAt": self._last_completed_at,
            "completedTasks": self._completed_log,
        }

    def _persist_sync(self) -> None:
        try:
            self._STATE_PATH.write_text(
                json.dumps(self._serialize_state(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    async def _persist_unlocked(self) -> None:
        payload = json.dumps(self._serialize_state(), indent=2)
        try:
            await asyncio.to_thread(
                self._STATE_PATH.write_text, payload, encoding="utf-8"
            )
        except OSError:
            pass

    def _stats_unlocked(self) -> Dict[str, Any]:
        active = len(self._tasks)
        return {
            "total": active + self._completed_total,
            "active": active,
            "completed": self._completed_total,
            "lastCompletedAt": self._last_completed_at,
        }

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "tasks": [asdict(task) for task in self._tasks.values()],
                "stats": self._stats_unlocked(),
                "completedTasks": list(self._completed_log),
            }

    async def add_task(self, title: str) -> Dict[str, Any]:
        new_task = TodoTask(id=uuid.uuid4().hex[:8], title=title.strip())
        async with self._lock:
            self._tasks[new_task.id] = new_task
            await self._persist_unlocked()
            return asdict(new_task)

    async def remove_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                return None
            self._completed_total += 1
            completed_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            self._last_completed_at = completed_at
            record = {
                "id": task.id,
                "title": task.title,
                "completed_at": completed_at,
            }
            self._completed_log.append(record)
            if len(self._completed_log) > 50:
                self._completed_log = self._completed_log[-50:]
            await self._persist_unlocked()
            return asdict(task)


TODO_STORE = TodoStore()


BUILD_DIR = Path(__file__).parent / "web" / "build"
ASSETS_DIR = BUILD_DIR / "static"


def _read_local_manifest() -> Dict[str, Any]:
    manifest_path = BUILD_DIR / "asset-manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Asset manifest not found at {manifest_path}. "
            "Run `yarn build` inside the web/ directory first."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _derive_asset_paths(manifest: Dict[str, Any]) -> Dict[str, Any]:
    entrypoints = manifest.get("entrypoints", [])

    css_entry = next((item for item in entrypoints if item.endswith(".css")), None)
    js_entry = next((item for item in entrypoints if item.endswith(".js")), None)

    if css_entry is None or js_entry is None:
        raise ValueError(
            "Could not determine CSS/JS entrypoints from asset manifest. "
            f"Entrypoints found: {entrypoints}"
        )

    css_rel = css_entry.lstrip("/")
    js_rel = js_entry.lstrip("/")

    version_token = Path(js_rel).stem.split(".")[-1]

    return {
        "css_rel": css_rel,
        "js_rel": js_rel,
        "version": version_token,
    }


def _fetch_remote_manifest(url: str) -> Optional[Dict[str, Any]]:
    try:
        response = httpx.get(url, timeout=3.0)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


USE_DEPLOYED_TEMPLATE = (
    os.environ.get("TODO_USE_DEPLOYED_ASSETS", "true").lower() == "true"
)

SERVER_URL = os.environ.get(
    "SERVER_URL",
    "https://cdn.jsdelivr.net/gh/Haniehz1/orange_todo_app@main/docs",
)


# Providing the JS and CSS to the app can be done in 1 of 2 ways:
# 1) Load the content as text from the static build files and inline them into the HTML template
# 2) (Preferred) Reference the static files served from the deployed server
# Since (2) depends on an initial deployment of the server, it is recommended to use approach (1) first
# and then switch to (2) once the server is deployed and its URL is available.
# (2) is preferred since (1) can lead to large HTML templates and potential for string escaping issues.


# Make sure these paths align with the build output paths (dynamic per build)
WIDGET_IDENTIFIER = "todo-dashboard"
WIDGET_TITLE = "Todo Dashboard"
# Legacy import-time globals removed: JS_PATH, CSS_PATH, CSS_URL, JS_URL.
MIME_TYPE = "text/html+skybridge"

def _tool_meta(widget: TodoWidget, read_only: bool = False) -> Dict[str, Any]:
    return {
        "openai/outputTemplate": widget.template_uri,
        "openai/toolInvocation/invoking": widget.invoking,
        "openai/toolInvocation/invoked": widget.invoked,
        "openai/widgetAccessible": True,
        "openai/resultCanProduceWidget": True,
        "annotations": {
            "destructiveHint": False,
            "openWorldHint": False,
            "readOnlyHint": read_only,
        },
    }



class AssetRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Minimal placeholder; first refresh() populates real assets.
        self._snapshot = TodoWidget(
            identifier=WIDGET_IDENTIFIER,
            title=WIDGET_TITLE,
            template_uri="ui://widget/todo-dashboard-initial.html",
            invoking="Loading your tasks",
            invoked="Loading your tasks...",
            html="<div id=\"todo-root\"></div>",
        )

    def _build_snapshot(self, manifest: Dict[str, Any]) -> TodoWidget:
        css_rel = manifest["css_rel"]
        js_rel = manifest["js_rel"]
        version = manifest["version"]

        deployed_html = (
            '<div id="todo-root"></div>\n'
            f'<link rel="stylesheet" href="{SERVER_URL}/{css_rel}">\n'
            f'<script type="module" src="{SERVER_URL}/{js_rel}"></script>'
        )

        # In deploys, prefer CDN assets to avoid local file dependency.
        # If you really want inline during local dev, add a guarded branch here.
        html = deployed_html

        return TodoWidget(
            identifier=WIDGET_IDENTIFIER,
            title=WIDGET_TITLE,
            template_uri=f"ui://widget/todo-dashboard-{version}.html",
            invoking="Checking your tasks",
            invoked="Updating your tasks...",
            html=html,
        )
    async def snapshot(self) -> TodoWidget:
        return self._snapshot

    async def refresh(self) -> TodoWidget:
        async with self._lock:
            if USE_DEPLOYED_TEMPLATE and SERVER_URL.startswith("http"):
                manifest_url = SERVER_URL.rstrip("/") + "/asset-manifest.json"
                manifest = _fetch_remote_manifest(manifest_url)
                if manifest:
                    self._snapshot = self._build_snapshot(
                        _derive_asset_paths(manifest)
                    )
                    return self._snapshot
            # fallback / initial load
            try:
                manifest = _derive_asset_paths(_read_local_manifest())
                self._snapshot = self._build_snapshot(manifest)
            except Exception:
                pass
            return self._snapshot


ASSET_REGISTRY = AssetRegistry()


mcp = FastMCP(
    name="todo",
    stateless_http=True,
)
app = MCPApp(
    name="todo",
    description="Interactive todo list widget with task management tools",
    mcp=mcp,
)


@mcp._mcp_server.list_tools()
async def _list_tools() -> List[types.Tool]:
    widget = await ASSET_REGISTRY.refresh()
    return [
        types.Tool(
            name=GET_TASKS_TOOL,
            title="Get tasks",
            inputSchema={"type": "object", "properties": {}},
            description="Retrieve the current todo list",
            _meta=_tool_meta(widget, read_only=True),
        ),
        types.Tool(
            name=ADD_TASK_TOOL,
            title="Add task",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short description of the task to add",
                    }
                },
                "required": ["title"],
            },
            description="Add a new task to the todo list",
            _meta=_tool_meta(widget, read_only=False),
        ),
        types.Tool(
            name=REMOVE_TASK_TOOL,
            title="Remove task",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Identifier of the task to remove",
                    }
                },
                "required": ["id"],
            },
            description="Remove a task from the todo list",
            _meta=_tool_meta(widget, read_only=False),
        ),
    ]


@mcp._mcp_server.list_resources()
async def _list_resources() -> List[types.Resource]:
    widget = await ASSET_REGISTRY.refresh()
    return [
        types.Resource(
            name=WIDGET_TITLE,
            title=WIDGET_TITLE,
            uri=widget.template_uri,
            description="Todo dashboard widget markup",
            mimeType=MIME_TYPE,
            _meta=_tool_meta(widget),
        )
    ]


@mcp._mcp_server.list_resource_templates()
async def _list_resource_templates() -> List[types.ResourceTemplate]:
    widget = await ASSET_REGISTRY.refresh()
    return [
        types.ResourceTemplate(
            name=WIDGET_TITLE,
            title=WIDGET_TITLE,
            uriTemplate=widget.template_uri,
            description="Todo dashboard widget markup",
            mimeType=MIME_TYPE,
            _meta=_tool_meta(widget),
        )
    ]


async def _handle_read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
    widget = await ASSET_REGISTRY.refresh()
    if str(req.params.uri) != widget.template_uri:
        return types.ServerResult(
            types.ReadResourceResult(
                contents=[],
                _meta={"error": f"Unknown resource: {req.params.uri}"},
            )
        )

    contents = [
        types.TextResourceContents(
            uri=widget.template_uri,
            mimeType=MIME_TYPE,
            text=widget.html,
            _meta=_tool_meta(widget),
        )
    ]

    return types.ServerResult(types.ReadResourceResult(contents=contents))


async def _call_tool_request(req: types.CallToolRequest) -> types.ServerResult:
    tool_name = req.params.name
    args = req.params.arguments or {}

    if tool_name not in {GET_TASKS_TOOL, ADD_TASK_TOOL, REMOVE_TASK_TOOL}:
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Unknown tool: {tool_name}",
                    )
                ],
                isError=True,
            )
        )

    widget = await ASSET_REGISTRY.refresh()
    widget_resource = types.EmbeddedResource(
        type="resource",
        resource=types.TextResourceContents(
            uri=widget.template_uri,
            mimeType=MIME_TYPE,
            text=widget.html,
            title=widget.title,
        ),
    )
    meta: Dict[str, Any] = {
        **_tool_meta(widget, read_only=tool_name == GET_TASKS_TOOL),
        "openai.com/widget": widget_resource.model_dump(mode="json"),
    }

    if tool_name == GET_TASKS_TOOL:
        snapshot = await TODO_STORE.snapshot()
        tasks = snapshot["tasks"]
        stats = snapshot["stats"]
        completed_log = snapshot.get("completedTasks", [])
        active = stats.get("active", 0)
        message = (
            "You currently have no active tasks."
            if active == 0
            else f"You currently have {active} active task{'s' if active != 1 else ''}."
        )
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structuredContent={
                    "tasks": tasks,
                    "stats": stats,
                    "completedTasks": completed_log,
                },
                _meta=meta,
            )
        )

    if tool_name == ADD_TASK_TOOL:
        title = str(args.get("title", "")).strip()
        if not title:
            return types.ServerResult(
                types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="Please provide a task title.")
                    ],
                    isError=True,
                    _meta=meta,
                )
            )

        new_task = await TODO_STORE.add_task(title)
        snapshot = await TODO_STORE.snapshot()
        tasks = snapshot["tasks"]
        stats = snapshot["stats"]
        completed_log = snapshot.get("completedTasks", [])
        message = (
            f"Added task “{new_task['title']}” (id: {new_task['id']}). "
            f"You now have {stats.get('active', 0)} active task"
            f"{'s' if stats.get('active', 0) != 1 else ''}."
        )
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structuredContent={
                    "tasks": tasks,
                    "stats": stats,
                    "completedTasks": completed_log,
                    "added": new_task,
                },
                _meta=meta,
            )
        )

    # Remove task tool
    task_id = str(args.get("id", "")).strip()
    if not task_id:
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text="Please provide the id of the task to remove."
                    )
                ],
                isError=True,
                _meta=meta,
            )
        )

    removed = await TODO_STORE.remove_task(task_id)
    if removed is None:
        snapshot = await TODO_STORE.snapshot()
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"No task with id “{task_id}” was found.",
                    )
                ],
                structuredContent={
                    "tasks": snapshot["tasks"],
                    "stats": snapshot["stats"],
                    "completedTasks": snapshot.get("completedTasks", []),
                },
                isError=True,
                _meta=meta,
            )
        )

    snapshot = await TODO_STORE.snapshot()
    tasks = snapshot["tasks"]
    stats = snapshot["stats"]
    completed_log = snapshot.get("completedTasks", [])
    remaining = stats.get("active", 0)
    completed = stats.get("completed", 0)
    message = (
        f"Completed task “{removed['title']}”. "
        f"{remaining} active remaining, {completed} completed overall."
    )
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            structuredContent={
                "tasks": tasks,
                "stats": stats,
                "completedTasks": completed_log,
                "removed": removed,
            },
            _meta=meta,
        )
    )


mcp._mcp_server.request_handlers[types.CallToolRequest] = _call_tool_request
mcp._mcp_server.request_handlers[types.ReadResourceRequest] = _handle_read_resource


# NOTE: This main function is for local testing; it spins up the MCP server (SSE) and
# serves the static assets for the web client. You can view the tool results / resources
# in MCP Inspector.
# Client development/testing should be done using the development webserver spun up via `yarn start`
# in the `web/` directory.
async def main():
    async with app.run() as todo_app:
        mcp_server = create_mcp_server_for_app(todo_app)

        ASSETS_DIR = BUILD_DIR / "static"
        if not ASSETS_DIR.exists():
            raise FileNotFoundError(
                f"Assets directory not found at {ASSETS_DIR}. "
                "Please build the web client before running the server."
            )

        starlette_app = mcp_server.sse_app()

        # This serves the static css and js files referenced by the HTML
        starlette_app.routes.append(
            Mount("/static", app=StaticFiles(directory=ASSETS_DIR), name="static")
        )

        # This serves the main HTML file at the root path for the server
        starlette_app.routes.append(
            Mount(
                "/",
                app=StaticFiles(directory=BUILD_DIR, html=True),
                name="root",
            )
        )

        # Serve via uvicorn, mirroring FastMCP.run_sse_async
        config = uvicorn.Config(
            starlette_app,
            host=mcp_server.settings.host,
            port=int(mcp_server.settings.port),
        )
        server = uvicorn.Server(config)
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
