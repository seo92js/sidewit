import { RotateCcw } from "lucide-react";
import { isRouteErrorResponse, useRouteError } from "react-router";

import { ErrorAlert } from "@/components/app/common";
import { Button } from "@/components/ui/button";

export function RouteError() {
  const error = useRouteError();
  return (
    <div className="mx-auto max-w-2xl space-y-3 p-6">
      <ErrorAlert
        error={
          isRouteErrorResponse(error)
            ? new Error(`${error.status} ${error.statusText}`.trim())
            : error || new Error("Unknown error")
        }
        title="Sidewit stopped because of an unexpected error"
        action={
          <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
            <RotateCcw /> Reload Sidewit
          </Button>
        }
      />
      <p className="text-sm text-muted-foreground">
        Reloading starts the page again. Changes that were not saved before this error cannot be
        recovered.
      </p>
    </div>
  );
}
