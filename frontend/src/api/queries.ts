import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { request } from "./client";
import type {
  Context,
  ContextCreate,
  ContextUpdate,
  ContextUpdateResult,
  GenerateRequest,
  GenerateResponse,
  GenerationDetail,
  Health,
  ImportDdlResult,
  ImportMode,
  InvokeResult,
  ListResponse,
  Tool,
  ToolCreate,
  ToolDraft,
  ToolUpdate,
  ValidateRequest,
  ValidationResult,
} from "./types";

const keys = {
  health: ["health"] as const,
  contexts: ["contexts"] as const,
  context: (id: string) => ["contexts", id] as const,
  tools: ["tools"] as const,
  tool: (name: string) => ["tools", name] as const,
  generation: (id: string) => ["generations", id] as const,
};

export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: () => request<Health>("GET", "/health"),
    refetchInterval: 30_000,
  });
}

// Contexts ---------------------------------------------------------------------------

export function useContexts() {
  return useQuery({
    queryKey: keys.contexts,
    queryFn: async () => (await request<ListResponse<Context>>("GET", "/contexts")).items,
  });
}

export function useContextDetail(id: string | undefined) {
  return useQuery({
    queryKey: keys.context(id ?? ""),
    queryFn: () => request<Context>("GET", `/contexts/${id}`),
    enabled: !!id,
  });
}

export function useCreateContext() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ContextCreate) => request<Context>("POST", "/contexts", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.contexts, exact: true }),
  });
}

export function useUpdateContext(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ContextUpdate) =>
      request<ContextUpdateResult>("PUT", `/contexts/${id}`, body),
    onSuccess: (result) => {
      qc.setQueryData(keys.context(id), result.context);
      qc.invalidateQueries({ queryKey: keys.contexts, exact: true });
      // Saving revalidates the context's tools, so their details change too.
      qc.invalidateQueries({ queryKey: keys.tools });
    },
  });
}

export function useDeleteContext() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<void>("DELETE", `/contexts/${id}`),
    // Not returned: navigation must not wait for the list refetch. Refetching the deleted
    // detail would only 404.
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: keys.context(id), exact: true });
      qc.invalidateQueries({ queryKey: keys.contexts, exact: true });
    },
  });
}

export function useImportDdl(id: string) {
  return useMutation({
    mutationFn: (body: { ddl: string; mode: ImportMode; expected_version: number }) =>
      request<ImportDdlResult>("POST", `/contexts/${id}/import-ddl`, body),
  });
}

// Generate ---------------------------------------------------------------------------

export function useGenerate() {
  return useMutation({
    mutationFn: (body: GenerateRequest) => request<GenerateResponse>("POST", "/generate", body),
  });
}

export function useGeneration(id: string | undefined) {
  return useQuery({
    queryKey: keys.generation(id ?? ""),
    queryFn: () => request<GenerationDetail>("GET", `/generations/${id}`),
    enabled: !!id,
  });
}

export function useFeedback() {
  return useMutation({
    mutationFn: ({ id, feedback }: { id: string; feedback: "up" | "down" }) =>
      request<unknown>("POST", `/generations/${id}/feedback`, { feedback }),
  });
}

// Tools ------------------------------------------------------------------------------

export function useTools() {
  return useQuery({
    queryKey: keys.tools,
    queryFn: async () => (await request<ListResponse<Tool>>("GET", "/tools")).items,
  });
}

export function useTool(name: string | undefined) {
  return useQuery({
    queryKey: keys.tool(name ?? ""),
    queryFn: () => request<Tool>("GET", `/tools/${name}`),
    enabled: !!name,
  });
}

export function useToolDraft(generationId: string | undefined) {
  return useQuery({
    queryKey: ["tool-draft", generationId],
    queryFn: () => request<ToolDraft>("POST", "/tools/draft", { generation_id: generationId }),
    enabled: !!generationId,
    staleTime: Infinity,
    retry: false,
  });
}

export function useCreateTool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ToolCreate) => request<Tool>("POST", "/tools", body),
    onSuccess: (tool) => {
      qc.setQueryData(keys.tool(tool.name), tool);
      qc.invalidateQueries({ queryKey: keys.tools, exact: true });
    },
  });
}

export function useUpdateTool(name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ToolUpdate) => request<Tool>("PUT", `/tools/${name}`, body),
    onSuccess: (tool) => {
      qc.setQueryData(keys.tool(name), tool);
      qc.invalidateQueries({ queryKey: keys.tools, exact: true });
    },
  });
}

export function useDeleteTool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => request<void>("DELETE", `/tools/${name}`),
    // As for contexts: drop the deleted detail instead of refetching it, and do not wait.
    onSuccess: (_, name) => {
      qc.removeQueries({ queryKey: keys.tool(name), exact: true });
      qc.invalidateQueries({ queryKey: keys.tools, exact: true });
    },
  });
}

/** Rebuild a metric-bound Tool's SQL and inputs from its current contract. */
export function useRefreshContract(name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (expected_version: number) =>
      request<Tool>("POST", `/tools/${name}/refresh-contract`, { expected_version }),
    onSuccess: (tool) => {
      qc.setQueryData(keys.tool(name), tool);
      qc.invalidateQueries({ queryKey: keys.tools, exact: true });
    },
  });
}

export function useInvokeTool(name: string, expectedVersion: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: Record<string, unknown>) =>
      request<InvokeResult>("POST", `/tools/${name}/invoke`, {
        arguments: args,
        expected_version: expectedVersion,
      }),
    // Invoke may revalidate a stale tool, which changes its status.
    onSettled: () => qc.invalidateQueries({ queryKey: keys.tool(name) }),
  });
}

/** Dry-run validation (DEFAULT policy). Not a Generate-compatibility check. */
export function validate(body: ValidateRequest) {
  return request<ValidationResult>("POST", "/validate", body);
}
