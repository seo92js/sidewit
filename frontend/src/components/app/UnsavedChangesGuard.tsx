import { useCallback } from "react";
import { useBeforeUnload, useBlocker } from "react-router";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

export function UnsavedChangesGuard({
  dirty,
  busy = false,
  description = "Your Context edits and any DDL not yet applied will be discarded.",
  allowNavigation,
}: {
  dirty: boolean;
  busy?: boolean;
  description?: string;
  allowNavigation?: (pathname: string) => boolean;
}) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      (dirty || busy) &&
      !allowNavigation?.(nextLocation.pathname) &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  useBeforeUnload(
    useCallback(
      (event) => {
        if (dirty || busy) {
          event.preventDefault();
          event.returnValue = "";
        }
      },
      [dirty, busy],
    ),
  );
  return (
    <Dialog
      open={blocker.state === "blocked"}
      onOpenChange={(open) => {
        if (!open && blocker.state === "blocked") blocker.reset();
      }}
    >
      <DialogContent>
        <DialogTitle>{busy ? "Request in progress" : "Leave without saving?"}</DialogTitle>
        <DialogDescription>
          {busy ? "Wait for the request to finish before leaving this editor." : description}
        </DialogDescription>
        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            onClick={() => {
              if (blocker.state === "blocked") blocker.reset();
            }}
          >
            Keep editing
          </Button>
          {!busy && (
            <Button
              variant="destructive"
              onClick={() => {
                if (blocker.state === "blocked") blocker.proceed();
              }}
            >
              Discard and leave
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
