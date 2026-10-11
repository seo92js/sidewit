// Bundles Monaco locally instead of loading it from a CDN (self-hosted, offline-friendly).
// All editor features (features/register.all: find, folding, diff editor, ...) but only the
// PostgreSQL grammar; the TypeScript/CSS/HTML/JSON language services and their workers are not
// bundled.
import "monaco-editor/features/register.all";
import "monaco-editor/languages/definitions/pgsql/register";

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";

self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });
