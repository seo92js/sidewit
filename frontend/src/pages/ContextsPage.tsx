import { Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useContexts, useCreateContext } from "@/api/queries";
import { Empty, ErrorAlert, Loading } from "@/components/app/common";
import { TimezoneInput } from "@/components/app/TimezoneInput";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

import { ContextEditor } from "./ContextEditor";

export default function ContextsPage() {
  const { id } = useParams();
  const contexts = useContexts();

  return (
    <div className="grid gap-5 md:grid-cols-[240px_1fr]">
      <aside className="space-y-3">
        <h1 className="text-lg font-semibold">Contexts</h1>
        {contexts.isLoading && <Loading />}
        {contexts.error && <ErrorAlert error={contexts.error} title="Could not load contexts" />}
        {contexts.data && contexts.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No contexts yet.</p>
        )}
        <ul className="space-y-0.5">
          {contexts.data?.map((c) => (
            <li key={c.id}>
              <Link
                to={`/contexts/${c.id}`}
                className={cn(
                  "flex items-center justify-between rounded-md px-2 py-1.5 text-sm",
                  c.id === id ? "bg-muted font-medium" : "hover:bg-muted/60",
                )}
              >
                <span className="truncate">{c.name}</span>
                <span className="font-mono text-xs text-muted-foreground">v{c.version}</span>
              </Link>
            </li>
          ))}
        </ul>
        <NewContextForm />
      </aside>
      <section className="min-w-0">
        {id ? (
          <ContextEditor key={id} id={id} />
        ) : (
          <Empty>Select a context, or create one and import its DDL.</Empty>
        )}
      </section>
    </div>
  );
}

function NewContextForm() {
  const navigate = useNavigate();
  const create = useCreateContext();
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  );

  return (
    <form
      className="space-y-2 rounded-md border p-3"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate(
          { name, dialect: "postgresql", timezone },
          {
            onSuccess: (context) => {
              setName("");
              navigate(`/contexts/${context.id}`);
            },
          },
        );
      }}
    >
      <div className="text-xs font-medium">New context</div>
      <div className="space-y-1">
        <Label htmlFor="new-name" className="text-xs">
          Name
        </Label>
        <Input
          id="new-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="commerce"
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="new-tz" className="text-xs">
          Timezone
        </Label>
        <TimezoneInput id="new-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
      </div>
      <p className="text-xs text-muted-foreground">Dialect: postgresql (v0.1)</p>
      <Button type="submit" size="sm" disabled={!name.trim() || create.isPending}>
        <Plus /> Create
      </Button>
      <ErrorAlert error={create.error} title="Could not create context" />
    </form>
  );
}
