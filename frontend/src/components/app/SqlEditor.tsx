import Editor from "@monaco-editor/react";
import { useCallback, useLayoutEffect, useRef } from "react";

import "./monaco";

interface Props {
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  height?: number | string;
}

// Hoisted so the editor does not receive new options on every render.
const OPTIONS = {
  readOnly: false,
  domReadOnly: false,
  minimap: { enabled: false },
  fontSize: 13,
  lineNumbers: "on",
  scrollBeyondLastLine: false,
  wordWrap: "on",
  renderLineHighlight: "line",
  automaticLayout: true,
  tabSize: 2,
} as const;
const READ_ONLY_OPTIONS = {
  ...OPTIONS,
  readOnly: true,
  domReadOnly: true,
  renderLineHighlight: "none",
} as const;

export default function SqlEditor({ value, onChange, readOnly = false, height = 220 }: Props) {
  // Parents pass inline callbacks; a stable handler keeps Monaco's change listener in place.
  const onChangeRef = useRef(onChange);
  useLayoutEffect(() => {
    onChangeRef.current = onChange;
  });
  const handleChange = useCallback((v: string | undefined) => onChangeRef.current?.(v ?? ""), []);

  return (
    <div className="overflow-hidden rounded-md border">
      <Editor
        height={height}
        language="pgsql"
        value={value}
        onChange={handleChange}
        options={readOnly ? READ_ONLY_OPTIONS : OPTIONS}
        loading={<div className="p-3 text-xs text-muted-foreground">Loading editor…</div>}
      />
    </div>
  );
}
