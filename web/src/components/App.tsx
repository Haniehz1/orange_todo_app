import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import "./App.css";
import { useTheme } from "src/utils/hooks/use-theme";
import { useWidgetState } from "src/utils/hooks/use-widget-state";
import { useOpenAiGlobal } from "src/utils/hooks/use-openai-global";
import type { TodoTask, TodoWidgetState } from "src/utils/types";

const GET_TASKS_TOOL = "todo-get-tasks";
const ADD_TASK_TOOL = "todo-add-task";
const REMOVE_TASK_TOOL = "todo-remove-task";

type ToolOutputPayload = {
  tasks?: TodoTask[];
};

function normalizeTasks(value: unknown): TodoTask[] | null {
  if (!Array.isArray(value)) {
    return null;
  }

  const filtered = value.filter(
    (item): item is TodoTask =>
      item &&
      typeof item === "object" &&
      typeof item.id === "string" &&
      typeof item.title === "string"
  );

  return filtered;
}

function App() {
  const theme = useTheme();
  const toolOutput = useOpenAiGlobal("toolOutput") as ToolOutputPayload | null;
  const [widgetState, setWidgetState] = useWidgetState<TodoWidgetState>(() => ({
    tasks: [],
  }));
  const [tasks, setTasks] = useState<TodoTask[]>(widgetState?.tasks ?? []);
  const [newTask, setNewTask] = useState("");
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [tasks]
  );

  const lastUpdatedDisplay = useMemo(() => {
    const timestamp =
      widgetState?.lastUpdated ??
      (sortedTasks.length ? sortedTasks[sortedTasks.length - 1].created_at : null);
    if (!timestamp) {
      return "Never";
    }
    return new Date(timestamp).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }, [sortedTasks, widgetState?.lastUpdated]);

  const syncTasks = useCallback(
    (next: TodoTask[]) => {
      setTasks(next);
      setWidgetState({
        tasks: next,
        lastUpdated: new Date().toISOString(),
      });
    },
    [setWidgetState]
  );

  useEffect(() => {
    if (widgetState?.tasks?.length) {
      setTasks(widgetState.tasks);
    }
  }, [widgetState]);

  useEffect(() => {
    const incoming = normalizeTasks(toolOutput?.tasks);
    if (incoming) {
      syncTasks(incoming);
    }
  }, [toolOutput, syncTasks]);

  const parseResult = useCallback(
    (rawResult: string | undefined) => {
      if (!rawResult) {
        return;
      }
      try {
        const parsed = JSON.parse(rawResult);
        const parsedTasks = normalizeTasks(parsed?.tasks);
        if (parsedTasks) {
          syncTasks(parsedTasks);
        }
      } catch {
        // swallow parse errors – some hosts may not return JSON text
      }
    },
    [syncTasks]
  );

  const runTool = useCallback(
    async (name: string, payload: Record<string, unknown>) => {
      if (typeof window === "undefined" || !window.openai?.callTool) {
        return;
      }

      setIsBusy(true);
      setError(null);
      try {
        const response = await window.openai.callTool(name, payload);
        parseResult(response?.result);
      } catch (err) {
        console.error("Tool call failed", err);
        setError("Something went wrong. Please try again.");
      } finally {
        setIsBusy(false);
      }
    },
    [parseResult]
  );

  useEffect(() => {
    // Load initial tasks from the server
    runTool(GET_TASKS_TOOL, {});
  }, [runTool]);

  const handleAddTask = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const title = newTask.trim();
      if (!title) {
        setError("Please enter a task before adding.");
        return;
      }

      setNewTask("");
      await runTool(ADD_TASK_TOOL, { title });
    },
    [newTask, runTool]
  );

  const handleRemoveTask = useCallback(
    async (taskId: string) => {
      await runTool(REMOVE_TASK_TOOL, { id: taskId });
    },
    [runTool]
  );

  return (
    <div className={`App ${theme}`} data-theme={theme}>
      <div className="todo-card" role="region" aria-label="Todo list">
        <header className="todo-card__header">
          <div className="todo-card__badge" aria-hidden>
            <span role="img" aria-label="Citrus check">
              🍊
            </span>
          </div>
          <h1>Todo List</h1>
          <p>Ground your day with a gentle plan.</p>
          <dl className="todo-card__meta">
            <div>
              <dt>Tasks</dt>
              <dd>{sortedTasks.length}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{lastUpdatedDisplay}</dd>
            </div>
          </dl>
        </header>

        <form className="todo-form" onSubmit={handleAddTask}>
          <input
            className="todo-form__input"
            type="text"
            placeholder="Add a task…"
            value={newTask}
            onChange={(event) => setNewTask(event.target.value)}
            disabled={isBusy}
            aria-label="Task title"
          />
          <button className="todo-form__button" type="submit" disabled={isBusy}>
            Add
          </button>
        </form>

        {error && <div className="todo-alert">{error}</div>}

        <ul className="todo-list">
          {sortedTasks.map((task) => (
            <li key={task.id} className="todo-item">
              <span className="todo-item__accent" aria-hidden />
              <div className="todo-item__body">
                <span className="todo-item__title">{task.title}</span>
                <span className="todo-item__timestamp">
                  Added {new Date(task.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                </span>
              </div>
              <button
                className="todo-item__remove"
                type="button"
                onClick={() => handleRemoveTask(task.id)}
                disabled={isBusy}
                aria-label={`Remove ${task.title}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>

        {sortedTasks.length === 0 && (
          <div className="todo-empty">
            <span className="todo-empty__emoji" role="img" aria-hidden>
              🍊
            </span>
            <p>Nothing here yet. Add your first task!</p>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
