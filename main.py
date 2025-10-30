"""Basic MCP mcp-agent app integration with OpenAI Apps SDK.

The server exposes widget-backed tools that render the UI bundle within the
client directory. Each handler returns the HTML shell via an MCP resource and
returns structured content so the ChatGPT client can hydrate the widget."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
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
from pathlib import Path

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
    """Simple in-memory task store for demo purposes."""

    def __init__(self) -> None:
        self._tasks: Dict[str, TodoTask] = {
            task.id: task
            for task in [
                TodoTask(id="welcome", title="Welcome to your orange todo list"),
                TodoTask(id="explore", title="Add a new task with the add task tool"),
                TodoTask(id="clean-up", title="Remove a task using the remove task tool"),
            ]
        }
        self._lock = asyncio.Lock()

    async def list_tasks(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return [asdict(task) for task in self._tasks.values()]

    async def add_task(self, title: str) -> Dict[str, Any]:
        new_task = TodoTask(id=uuid.uuid4().hex[:8], title=title.strip())
        async with self._lock:
            self._tasks[new_task.id] = new_task
            return asdict(new_task)

    async def remove_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            task = self._tasks.pop(task_id, None)
            return asdict(task) if task else None


TODO_STORE = TodoStore()


BUILD_DIR = Path(__file__).parent / "web" / "build"
ASSETS_DIR = BUILD_DIR / "static"


def _load_asset_paths() -> Dict[str, Path]:
    """Read CRA asset-manifest to find the hashed CSS/JS bundle names."""
    manifest_path = BUILD_DIR / "asset-manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Asset manifest not found at {manifest_path}. "
            "Run `yarn build` inside the web/ directory first."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entrypoints = manifest.get("entrypoints", [])

    css_entry = next((item for item in entrypoints if item.endswith(".css")), None)
    js_entry = next((item for item in entrypoints if item.endswith(".js")), None)

    if css_entry is None or js_entry is None:
        raise ValueError(
            "Could not determine CSS/JS entrypoints from asset-manifest.json. "
            f"Entrypoints found: {entrypoints}"
        )

    css_rel = css_entry.lstrip("/")
    js_rel = js_entry.lstrip("/")

    css_path = BUILD_DIR / css_rel
    js_path = BUILD_DIR / js_rel

    if not css_path.exists() or not js_path.exists():
        raise FileNotFoundError(
            f"Expected build assets not found. CSS: {css_path.exists()} JS: {js_path.exists()}"
        )

    return {"css": css_path, "js": js_path, "css_rel": css_rel, "js_rel": js_rel}


ASSET_PATHS = _load_asset_paths()

# Providing the JS and CSS to the app can be done in 1 of 2 ways:
# 1) Load the content as text from the static build files and inline them into the HTML template
# 2) (Preferred) Reference the static files served from the deployed server
# Since (2) depends on an initial deployment of the server, it is recommended to use approach (1) first
# and then switch to (2) once the server is deployed and its URL is available.
# (2) is preferred since (1) can lead to large HTML templates and potential for string escaping issues.


# Make sure these paths align with the build output paths (dynamic per build)
JS_PATH = ASSET_PATHS["js"]
CSS_PATH = ASSET_PATHS["css"]

# METHOD 1: Inline the JS and CSS into the HTML template
WIDGET_JS = JS_PATH.read_text(encoding="utf-8")
WIDGET_CSS = CSS_PATH.read_text(encoding="utf-8")

INLINE_HTML_TEMPLATE = f"""
<div id="todo-root"></div>
<style>
{WIDGET_CSS}
</style>
<script type="module">
{WIDGET_JS}
</script>
"""

# METHOD 2: Reference the static files from the deployed server
SERVER_URL = os.environ.get(
    "SERVER_URL",
    "https://cdn.jsdelivr.net/gh/Haniehz1/orange_todo_app@main/docs",
)
CSS_URL = "/" + ASSET_PATHS["css_rel"]
JS_URL = "/" + ASSET_PATHS["js_rel"]
DEPLOYED_HTML_TEMPLATE = (
    '<div id="todo-root"></div>\n'
    f'<link rel="stylesheet" href="{SERVER_URL}{CSS_URL}">\n'
    f'<script type="module" src="{SERVER_URL}{JS_URL}"></script>'
)

USE_DEPLOYED_TEMPLATE = (
    os.environ.get("TODO_USE_DEPLOYED_ASSETS", "true").lower() == "true"
)
HTML_TEMPLATE = DEPLOYED_HTML_TEMPLATE if USE_DEPLOYED_TEMPLATE else INLINE_HTML_TEMPLATE

WIDGET = TodoWidget(
    identifier="todo-dashboard",
    title="Todo Dashboard",
    # OpenAI Apps heavily cache resources by URI, so use a date-based URI to bust the cache when updating the app.
    template_uri="ui://widget/todo-dashboard-10-29-2025-00-05.html",
    invoking="Checking your tasks",
    invoked="Updating your tasks...",
    html=HTML_TEMPLATE,
)

GET_TASKS_TOOL = "todo-get-tasks"
ADD_TASK_TOOL = "todo-add-task"
REMOVE_TASK_TOOL = "todo-remove-task"


MIME_TYPE = "text/html+skybridge"

mcp = FastMCP(
    name="todo",
    stateless_http=True,
)
app = MCPApp(
    name="todo",
    description="Interactive todo list widget with task management tools",
    mcp=mcp,
)


def _resource_description() -> str:
    return "Todo dashboard widget markup"


def _tool_meta(read_only: bool = False) -> Dict[str, Any]:
    return {
        "openai/outputTemplate": WIDGET.template_uri,
        "openai/toolInvocation/invoking": WIDGET.invoking,
        "openai/toolInvocation/invoked": WIDGET.invoked,
        "openai/widgetAccessible": True,
        "openai/resultCanProduceWidget": True,
        "annotations": {
            "destructiveHint": False,
            "openWorldHint": False,
            "readOnlyHint": read_only,
        },
    }


def _embedded_widget_resource() -> types.EmbeddedResource:
    return types.EmbeddedResource(
        type="resource",
        resource=types.TextResourceContents(
            uri=WIDGET.template_uri,
            mimeType=MIME_TYPE,
            text=WIDGET.html,
            title=WIDGET.title,
        ),
    )


@mcp._mcp_server.list_tools()
async def _list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name=GET_TASKS_TOOL,
            title="Get tasks",
            inputSchema={"type": "object", "properties": {}},
            description="Retrieve the current todo list",
            _meta=_tool_meta(read_only=True),
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
            _meta=_tool_meta(read_only=False),
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
            _meta=_tool_meta(read_only=False),
        ),
    ]


@mcp._mcp_server.list_resources()
async def _list_resources() -> List[types.Resource]:
    return [
        types.Resource(
            name=WIDGET.title,
            title=WIDGET.title,
            uri=WIDGET.template_uri,
            description=_resource_description(),
            mimeType=MIME_TYPE,
            _meta=_tool_meta(),
        )
    ]


@mcp._mcp_server.list_resource_templates()
async def _list_resource_templates() -> List[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            name=WIDGET.title,
            title=WIDGET.title,
            uriTemplate=WIDGET.template_uri,
            description=_resource_description(),
            mimeType=MIME_TYPE,
            _meta=_tool_meta(),
        )
    ]


async def _handle_read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
    if str(req.params.uri) != WIDGET.template_uri:
        return types.ServerResult(
            types.ReadResourceResult(
                contents=[],
                _meta={"error": f"Unknown resource: {req.params.uri}"},
            )
        )

    contents = [
        types.TextResourceContents(
            uri=WIDGET.template_uri,
            mimeType=MIME_TYPE,
            text=WIDGET.html,
            _meta=_tool_meta(),
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

    widget_resource = _embedded_widget_resource()
    meta: Dict[str, Any] = {
        **_tool_meta(read_only=tool_name == GET_TASKS_TOOL),
        "openai.com/widget": widget_resource.model_dump(mode="json"),
    }

    if tool_name == GET_TASKS_TOOL:
        tasks = await TODO_STORE.list_tasks()
        message = (
            "You currently have no tasks."
            if not tasks
            else f"You currently have {len(tasks)} task{'s' if len(tasks) != 1 else ''}."
        )
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structuredContent={"tasks": tasks},
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
        tasks = await TODO_STORE.list_tasks()
        message = f"Added task “{new_task['title']}” (id: {new_task['id']})."
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structuredContent={"tasks": tasks, "added": new_task},
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
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"No task with id “{task_id}” was found.",
                    )
                ],
                isError=True,
                _meta=meta,
            )
        )

    tasks = await TODO_STORE.list_tasks()
    message = f"Removed task “{removed['title']}”."
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            structuredContent={"tasks": tasks, "removed": removed},
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
