// Public API DTOs (SPEC §3, §5). Mirrors backend response/request models.

export type Dialect = "postgresql";
export type ParamType = "string" | "integer" | "number" | "boolean" | "date" | "date-time";
export type ScalarValue = string | number | boolean;

export interface Issue {
  code: string;
  message: string;
  ref: string | null;
}

export interface ContextField {
  name: string;
  type: string;
  description?: string | null;
  primary_key?: boolean;
  nullable?: boolean;
  /** Every value an enum-like field holds; generated SQL and Tool inputs must use one exactly. */
  allowed_values?: ScalarValue[];
}

export interface Resource {
  name: string;
  description?: string | null;
  fields: ContextField[];
}

export interface Relation {
  from: string;
  to: string;
}

/** A business constant as data, e.g. rentals.state = 'SETTLED'. */
export interface FixedCondition {
  field: string;
  op?: Comparison;
  value: ScalarValue;
}

export interface Rule {
  id: string;
  text: string;
  /** Server-enforced form of the rule's constant; generated SQL keeps it a literal. */
  condition?: FixedCondition | null;
}

export interface ContextDefinition {
  resources: Resource[];
  relations: Relation[];
  rules: Rule[];
  metrics?: MetricDefinition[];
}

export type Comparison = "=" | "!=" | ">" | ">=" | "<" | "<=";
export interface MetricDefinition {
  id: string;
  description: string;
  resource: string;
  aggregate: {
    kind: "row_count" | "non_null_count" | "sum" | "avg" | "min" | "max";
    field?: string | null;
    distinct?: boolean;
  };
  joins?: Relation[];
  group_by?: string[];
  fixed_filters?: { field: string; op?: Comparison; value: ScalarValue }[];
  inputs?: {
    name: string;
    type: ParamType;
    field: string;
    op?: Comparison;
    description?: string | null;
  }[];
  time_fields?: string[];
  default_time_field?: string | null;
}
export interface MetricBinding {
  metric_id: string;
  time_field: string | null;
}

/**
 * Where an ok operation came from. metric: built by the server from a registered metric
 * contract. generated_sql: written by the model and structurally validated; review its meaning.
 */
export type OperationSource = "metric" | "generated_sql";

export interface Context {
  id: string;
  name: string;
  dialect: Dialect;
  timezone: string;
  /** Registered metrics are tried first; this allows model-written SQL when none fits. */
  allow_generated_sql: boolean;
  definition: ContextDefinition;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ContextCreate {
  name: string;
  dialect: Dialect;
  timezone?: string;
  allow_generated_sql?: boolean;
  definition?: ContextDefinition;
}

export interface ContextUpdate {
  name: string;
  dialect: Dialect;
  timezone: string;
  allow_generated_sql: boolean;
  definition: ContextDefinition;
  expected_version: number;
}

export interface Revalidation {
  checked: number;
  valid: string[];
  invalid: { name: string; errors: Issue[] }[];
}

export interface ContextUpdateResult {
  context: Context;
  revalidation: Revalidation;
}

export type ImportMode = "merge" | "replace";

export interface PendingForeignKey {
  table: string;
  column: string;
  ref_table: string;
  ref_column: string | null;
}

export interface ImportDdlRequest {
  ddl: string;
  mode: ImportMode;
  expected_version: number;
  base_definition: ContextDefinition;
  pending_foreign_keys: PendingForeignKey[];
}

export interface ImportDdlResult {
  context_version: number;
  definition: ContextDefinition;
  warnings: string[];
  pending_foreign_keys?: PendingForeignKey[];
}

export interface ValidationResult {
  valid: boolean;
  errors: Issue[];
  warnings: Issue[];
  checks: string[];
}

export interface SqlOperation {
  type: "sql";
  dialect: Dialect;
  statement: string;
  parameters: Record<string, unknown>;
  parameter_types: Record<string, ParamType>;
}

export interface ToolSuggestion {
  name: string;
  description: string;
}

export interface GenerateRequest {
  context_id: string;
  question: string;
  reference_time?: string;
  metric_id?: string;
  date_field?: string;
  /** Answer only with a registered metric for this request. */
  require_metric?: boolean;
}

export type GenerateResponse =
  | {
      status: "ok";
      source: OperationSource;
      generation_id: string;
      context_version: number;
      operation: SqlOperation;
      validation: ValidationResult;
      tool_suggestion: ToolSuggestion | null;
      metric_binding?: MetricBinding | null;
    }
  | {
      status: "ambiguous";
      generation_id: string;
      context_version: number;
      reason: string;
      clarifications: string[];
    }
  | {
      status: "unanswerable";
      generation_id: string;
      context_version: number;
      reason: string;
    };

export interface GenerationDetail {
  id: string;
  source: OperationSource | null;
  context_id: string;
  context_version: number;
  question: string;
  status: "ok" | "ambiguous" | "unanswerable" | "failed";
  feedback: "up" | "down" | null;
  provider: string;
  model: string;
  prompt_version: string;
  attempts: number;
  latency_ms: number;
  created_at: string;
  reference_time: string;
  operation: SqlOperation | null;
  reason: string | null;
  clarifications: string[] | null;
  tool_suggestion: ToolSuggestion | null;
  validation: ValidationResult | null;
  input_tokens: number | null;
  output_tokens: number | null;
  metric_binding?: MetricBinding | null;
}

// Tool input contract (SPEC §4.1).
export interface InputProperty {
  type: "string" | "integer" | "number" | "boolean";
  format?: "date" | "date-time";
  description?: string;
  enum?: string[];
}

export interface InputSchema {
  type: "object";
  properties: Record<string, InputProperty>;
  required: string[];
  additionalProperties: false;
}

export interface ToolOperation {
  type: "sql";
  statement: string;
}

export type ToolStatus = "valid" | "invalid" | "stale";

export interface Tool {
  id: string;
  context_id: string;
  name: string;
  description: string;
  input_schema: InputSchema;
  operation: ToolOperation;
  version: number;
  context_version: number;
  status: ToolStatus;
  validation_errors: Issue[];
  validation: ValidationResult | null;
  source_generation_id: string | null;
  metric_binding?: MetricBinding | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCreate {
  context_id: string;
  name: string;
  description: string;
  input_schema: InputSchema;
  operation: ToolOperation;
  source_generation_id?: string | null;
  metric_binding?: MetricBinding | null;
}

export interface ToolUpdate {
  expected_version: number;
  description: string;
  input_schema: InputSchema;
  operation: ToolOperation;
}

export interface ToolDraft {
  context_id: string;
  name: string | null;
  description: string;
  input_schema: InputSchema;
  operation: ToolOperation;
  example_arguments: Record<string, unknown>;
  source_generation_id: string;
  metric_binding?: MetricBinding | null;
}

export interface InvokeResult {
  validation: ValidationResult;
  tool: string;
  tool_version: number;
  context_version: number;
  operation: SqlOperation;
}

export interface ValidateRequest {
  context_id: string;
  operation: ToolOperation;
  input_schema?: InputSchema;
  metric_binding?: MetricBinding | null;
}

export interface Health {
  status: string;
  storage: string;
}

export interface ListResponse<T> {
  items: T[];
}

// Error envelope (SPEC §5).
export interface ApiErrorBody {
  error: string;
  message: string;
  details: Record<string, unknown>;
}
