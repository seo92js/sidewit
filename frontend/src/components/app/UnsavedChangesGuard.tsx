import { useCallback } from "react";
import { useBeforeUnload, useBlocker } from "react-router";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

export function UnsavedChangesGuard({ dirty }: { dirty: boolean }) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  useBeforeUnload(
    useCallback(
      (event) => {
        if (dirty) {
          event.preventDefault();
          event.returnValue = "";
        }
      },
      [dirty],
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
        <DialogTitle>Leave without saving?</DialogTitle>
        <DialogDescription>
          Your Context edits and any DDL not yet applied will be discarded.
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
          <Button
            variant="destructive"
            onClick={() => {
              if (blocker.state === "blocked") blocker.proceed();
            }}
          >
            Discard and leave
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
