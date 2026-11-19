import {
  Component,
  lazy,
  Suspense,
  useState,
  type ComponentType,
  type LazyExoticComponent,
  type ReactNode,
} from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

import type { SqlEditorProps } from "./SqlEditor";

type EditorModule = { default: ComponentType<SqlEditorProps> };
type Editor = LazyExoticComponent<ComponentType<SqlEditorProps>>;

/** Keeps the page and its edits when the editor fails to load. */
class EditorBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

function PlainSqlEditor({
  value,
  onChange,
  readOnly = false,
  height = 220,
  onRetry,
}: SqlEditorProps & { onRetry: () => void }) {
  const locked = readOnly || !onChange;
  return (
    <div className="space-y-2 rounded-md border p-2">
      <div
        role="alert"
        className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"
      >
        <span>
          The SQL editor could not be loaded.{" "}
          {locked ? "Showing the SQL as plain text." : "You can keep editing as plain text."}
        </span>
        <Button type="button" size="xs" variant="outline" onClick={onRetry}>
          Retry loading editor
        </Button>
      </div>
      <Textarea
        aria-label="SQL (plain text)"
        className="field-sizing-fixed font-mono text-xs"
        style={{ height }}
        spellCheck={false}
        value={value}
        readOnly={locked}
        onChange={locked ? undefined : (event) => onChange?.(event.target.value)}
      />
    </div>
  );
}

/** React.lazy caches a rejected import, so a retry needs a new lazy component. */
export function createLazySqlEditor(load: () => Promise<EditorModule>) {
  const shared = { editor: lazy(load) as Editor };

  return function LazySqlEditor(props: SqlEditorProps) {
    const [attempt, setAttempt] = useState(() => ({ editor: shared.editor, count: 0 }));
    const retry = () => {
      // Another editor on the page may already have started a newer attempt.
      if (shared.editor === attempt.editor) shared.editor = lazy(load);
      setAttempt({ editor: shared.editor, count: attempt.count + 1 });
    };
    const Current = attempt.editor;
    return (
      <EditorBoundary key={attempt.count} fallback={<PlainSqlEditor {...props} onRetry={retry} />}>
        <Suspense
          fallback={
            <div className="rounded-md border p-3 text-xs text-muted-foreground">
              Loading editor…
            </div>
          }
        >
          <Current {...props} />
        </Suspense>
      </EditorBoundary>
    );
  };
}

export const LazySqlEditor = createLazySqlEditor(() => import("./SqlEditor"));
