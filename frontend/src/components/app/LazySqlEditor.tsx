import { lazy, Suspense, type ComponentProps } from "react";

const SqlEditor = lazy(() => import("./SqlEditor"));

/** Monaco is large; load it only on screens that show SQL. */
export function LazySqlEditor(props: ComponentProps<typeof SqlEditor>) {
  return (
    <Suspense
      fallback={
        <div className="rounded-md border p-3 text-xs text-muted-foreground">Loading editor…</div>
      }
    >
      <SqlEditor {...props} />
    </Suspense>
  );
}
