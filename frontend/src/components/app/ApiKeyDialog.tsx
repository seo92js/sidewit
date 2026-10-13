import { useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";

import { getApiKey, setApiKey } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function ApiKeyDialog() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(""); // loaded from storage on open
  const hasKey = !!getApiKey();

  const save = (key: string) => {
    setApiKey(key.trim());
    setOpen(false);
    qc.invalidateQueries();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setValue(getApiKey());
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <KeyRound /> {hasKey ? "Sidewit API key set" : "Sidewit API key"}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Sidewit API key</DialogTitle>
          <DialogDescription>
            Authenticates requests to Sidewit. Enter the value configured on your server as{" "}
            <code>SIDEWIT_API_KEY</code>; leave empty if it is not configured. Gemini/OpenAI keys
            belong in the server configuration, not here. Stored in this browser's localStorage and
            sent as <code>Authorization: Bearer</code>. A v0.1 convenience for local/self-hosted
            use, not a long-term credential store.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="api-key">Sidewit API key</Label>
          <Input
            id="api-key"
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save(value)}
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => save("")}>
            Clear
          </Button>
          <Button onClick={() => save(value)}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
