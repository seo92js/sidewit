import { Navigate, NavLink, Route, Routes } from "react-router";

import { useHealth } from "@/api/queries";
import { ApiKeyDialog } from "@/components/app/ApiKeyDialog";
import { cn } from "@/lib/utils";
import ContextsPage from "@/pages/ContextsPage";
import PlaygroundPage from "@/pages/PlaygroundPage";
import ToolEditorPage from "@/pages/ToolEditorPage";
import ToolsPage from "@/pages/ToolsPage";

const NAV = [
  { to: "/playground", label: "Playground" },
  { to: "/contexts", label: "Contexts" },
  { to: "/tools", label: "Tools" },
];

function HealthDot() {
  const { data, isError } = useHealth();
  const ok = data?.status === "ok";
  return (
    <span
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      title={
        isError ? "API unreachable" : `API ${data?.status ?? "…"} · storage ${data?.storage ?? "…"}`
      }
    >
      <span
        className={cn(
          "size-2 rounded-full",
          ok ? "bg-emerald-500" : isError ? "bg-red-500" : "bg-muted-foreground/40",
        )}
      />
      API
    </span>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-12 max-w-7xl items-center gap-6 px-4">
          <span className="font-semibold tracking-tight">Sidewit</span>
          <nav className="flex gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-2.5 py-1 text-sm",
                    isActive
                      ? "bg-muted font-medium"
                      : "text-muted-foreground hover:text-foreground",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <HealthDot />
            <ApiKeyDialog />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-5">
        <Routes>
          <Route path="/" element={<Navigate to="/playground" replace />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/contexts" element={<ContextsPage />} />
          <Route path="/contexts/:id" element={<ContextsPage />} />
          <Route path="/tools" element={<ToolsPage />} />
          <Route path="/tools/new" element={<ToolEditorPage />} />
          <Route path="/tools/:id" element={<ToolEditorPage />} />
          <Route path="*" element={<Navigate to="/playground" replace />} />
        </Routes>
      </main>
    </div>
  );
}
