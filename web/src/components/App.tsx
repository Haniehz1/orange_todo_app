import { useEffect, useMemo, useState } from "react";
import "./App.css";
import { useTheme } from "src/utils/hooks/use-theme";
import { useOpenAiGlobal } from "src/utils/hooks/use-openai-global";
import type { CompletedTask, TodoStats, TodoTask } from "src/utils/types";

type ToolOutputPayload = {
  tasks?: TodoTask[];
  stats?: TodoStats;
  completedTasks?: CompletedTask[];
  added?: TodoTask;
  removed?: TodoTask;
};

const MODE: "get" | "add" | "remove" =
  (typeof window !== "undefined" &&
    ((window as unknown as { __TODO_WIDGET_MODE__?: string })
      .__TODO_WIDGET_MODE__ as "get" | "add" | "remove" | undefined)) ||
  "get";

const DEFAULT_STATS: TodoStats = { total: 0, active: 0, completed: 0 };

const DEFAULT_SNAPSHOT = {
  tasks: [] as TodoTask[],
  stats: DEFAULT_STATS,
  completed: [] as CompletedTask[],
  added: null as TodoTask | null,
  removed: null as TodoTask | null,
};

function normalizeTasks(value: unknown): TodoTask[] | null {
  if (!Array.isArray(value)) {
    return null;
  }

  return value
    .map((item) => normalizeTask(item))
    .filter((item): item is TodoTask => item != null);
}

function normalizeStats(value: unknown): TodoStats | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const stats = value as Record<string, unknown>;
  const total = Number(stats.total ?? 0);
  const active = Number(stats.active ?? 0);
  const completed = Number(stats.completed ?? 0);
  return {
    total: Number.isFinite(total) ? total : 0,
    active: Number.isFinite(active) ? active : 0,
    completed: Number.isFinite(completed) ? completed : 0,
    lastCompletedAt:
      typeof stats.lastCompletedAt === "string"
        ? stats.lastCompletedAt
        : undefined,
  };
}

function normalizeCompleted(value: unknown): CompletedTask[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  return value
    .map((entry) => {
      if (!entry || typeof entry !== "object") {
        return null;
      }
      const record = entry as Record<string, unknown>;
      if (
        typeof record.id === "string" &&
        typeof record.title === "string" &&
        typeof record.completed_at === "string"
      ) {
        return {
          id: record.id,
          title: record.title,
          completed_at: record.completed_at,
        } satisfies CompletedTask;
      }
      return null;
    })
    .filter((entry): entry is CompletedTask => entry != null);
}

function normalizeTask(value: unknown): TodoTask | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.id === "string" && typeof record.title === "string") {
    const created =
      typeof record.created_at === "string"
        ? record.created_at
        : new Date().toISOString();
    return {
      id: record.id,
      title: record.title,
      created_at: created,
      done: Boolean(record.done),
    } satisfies TodoTask;
  }
  return null;
}

function formatTimestamp(value?: string | null): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function App() {
  const theme = useTheme();
  const toolOutput = useOpenAiGlobal("toolOutput") as ToolOutputPayload | null;

  const [snapshot, setSnapshot] = useState(DEFAULT_SNAPSHOT);

  useEffect(() => {
    if (!toolOutput) {
      return;
    }

    setSnapshot((prev) => {
      const tasks = normalizeTasks(toolOutput.tasks) ?? prev.tasks;
      const stats = normalizeStats(toolOutput.stats) ?? prev.stats;
      const completed = normalizeCompleted(toolOutput.completedTasks) ?? prev.completed;
      const added = normalizeTask(toolOutput.added);
      const removed = normalizeTask(toolOutput.removed);

      return {
        tasks,
        stats,
        completed,
        added: added ?? (MODE === "add" ? prev.added : null),
        removed: removed ?? (MODE === "remove" ? prev.removed : null),
      };
    });
  }, [toolOutput]);

  const { tasks, stats, completed, added, removed } = snapshot;

  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [tasks]
  );

  const recentCompletions = useMemo(
    () => [...completed].sort((a, b) => b.completed_at.localeCompare(a.completed_at)).slice(0, 5),
    [completed]
  );

  const headerTitle =
    MODE === "add"
      ? "Task Added"
      : MODE === "remove"
      ? "Task Completed"
      : "Todo Overview";

  const headerSubtitle =
    MODE === "add"
      ? "Captured and saved your new todo."
      : MODE === "remove"
      ? "Checked off and logged for your records."
      : "Review what’s on deck and what’s done.";

  return (
    <div className={`App ${theme}`} data-theme={theme}>
      <section className={`todo-card todo-card--${MODE}`} aria-label="Todo widget">
        <header className="todo-card__header">
          <div className="todo-card__badge" aria-hidden>
            <span role="img" aria-label="Citrus check">
              🍊
            </span>
          </div>
          <h1>{headerTitle}</h1>
          <p>{headerSubtitle}</p>
        </header>

        <div className="todo-summary" aria-label="Summary">
          <div>
            <span className="todo-summary__label">Active</span>
            <span className="todo-summary__value">{stats.active}</span>
          </div>
          <div>
            <span className="todo-summary__label">Completed</span>
            <span className="todo-summary__value">{stats.completed}</span>
          </div>
          <div>
            <span className="todo-summary__label">Total</span>
            <span className="todo-summary__value">{stats.total}</span>
          </div>
        </div>

        {MODE === "add" && added && (
          <section className="todo-section" aria-label="New task">
            <h2>New task saved</h2>
            <div className="todo-highlight">
              <span className="todo-highlight__title">{added.title}</span>
              <span className="todo-highlight__meta">
                Added {formatTimestamp(added.created_at)}
              </span>
            </div>
          </section>
        )}

        {MODE === "remove" && removed && (
          <section className="todo-section" aria-label="Completed task">
            <h2>Marked complete</h2>
            <div className="todo-highlight todo-highlight--complete">
              <span className="todo-highlight__title">{removed.title}</span>
              <span className="todo-highlight__meta">
                Completed {formatTimestamp(stats.lastCompletedAt)}
              </span>
            </div>
          </section>
        )}

        {MODE === "get" && (
          <section className="todo-section" aria-label="Active tasks">
            <h2>Active tasks</h2>
            {sortedTasks.length > 0 ? (
              <ul className="todo-list">
                {sortedTasks.map((task) => (
                  <li key={task.id} className="todo-item">
                    <span className="todo-item__accent" aria-hidden />
                    <div className="todo-item__body">
                      <span className="todo-item__title">{task.title}</span>
                      <span className="todo-item__timestamp">
                        Added {formatTimestamp(task.created_at)}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="todo-empty">You’re all clear for now.</p>
            )}
          </section>
        )}

        {recentCompletions.length > 0 && (
          <section className="todo-section" aria-label="Recent completions">
            <h2>Recent completions</h2>
            <ol className="todo-timeline">
              {recentCompletions.map((entry) => (
                <li key={`${entry.id}-${entry.completed_at}`}>
                  <span className="todo-timeline__title">{entry.title}</span>
                  <span className="todo-timeline__meta">
                    {formatTimestamp(entry.completed_at)}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}

        <footer className="todo-footer">
          <p>
            Use the add or remove tools to update your list, then call “Get tasks” to
            refresh this snapshot.
          </p>
        </footer>
      </section>
    </div>
  );
}

export default App;
