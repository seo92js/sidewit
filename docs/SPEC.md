# Sidewit v0.1 SPEC

> Sidewit is an AI Worker, not a database proxy.
> Sidewit turns context and natural-language intent into reviewable SQL operations.
> Your application stays in control of execution.

이 문서는 Sidewit v0.1의 구현 기준이다. 이 문서와 다르게 구현해야 할 이유가 생기면 구현 전에 문서를 먼저 고친다.

---

## 0. 핵심 원칙

1. **Sidewit의 책임 경계.**
   - **Generate**는 Context와 질문으로 **검토할 Operation 초안**을 만든다. 무엇을 검사했고 무엇을 확인하지 못했는지 함께 반환한다.
   - **Tool**은 검토해 저장한 Operation과 입력 계약을 재사용한다. 같은 입력에 결정론적으로 같은 Operation을 반환하지만, DB 실행 성공이나 업무 의미를 보증하지 않는다.
   - **실행 애플리케이션**은 실제 DB 권한, 사용자·테넌트 접근, 실행 여부, 실행 시간·자원 제한을 통제한다.
2. **Operation ≠ Tool.** Operation은 실행 계획이다. Tool은 검증된 Operation 템플릿에 입력 계약(JSON Schema)을 붙여 재사용 가능하게 만든 인터페이스다.
3. **Generated operations are probabilistic. Saved tools are deterministic.** `/generate`는 LLM을 쓴다. `/tools/{name}/invoke`는 LLM을 쓰지 않는다.
4. **Sidewit은 어떤 데이터베이스에도 연결하지 않는다.** Application DB credential을 받지 않고, SQL을 실행하거나 EXPLAIN하지 않는다. Sidewit 자신의 상태(Context, Tool)는 데이터 폴더의 파일로 저장한다(§11).
5. **Sidewit admits what it cannot answer.** 답할 수 없거나 모호한 질문에는 SQL을 만들지 않고 `unanswerable` 또는 `ambiguous`를 반환한다.
6. **Validation은 정적 검사다.** SQL을 파싱해 문장 종류, Context 범위, 이름, 파라미터 계약, 일부 값 정책을 검사한다(§7). 타입·함수의 실제 호환성, 실행 가능성, 실행 비용, 함수 부작용, Business Rule 준수는 보장하지 않는다. SELECT는 문장 종류일 뿐 무해함을 뜻하지 않는다. 정적 검사의 지원 범위는 넓히지 않으며, 필수 검사를 할 수 없으면 거부한다.
7. **LLM DTO ≠ Domain Model ≠ Public API DTO.** provider 편의를 위한 형식(§6.2)은 공개 API에 노출하지 않는다.

라이선스는 Apache-2.0이다.

---

## 1. 범위

### v0.1에 포함

- Data Context 관리: DDL import, 설명·관계·Business Rule·지표 계약 편집, 버전 관리
- `POST /generate`: 자연어를 parameterized SQL Operation 초안으로 변환하거나 ambiguous/unanswerable 반환
- 정적 SQL Validator
- Tool: Generation에서 초안 생성, 사용자 편집, 검증 후 저장, 결정론적 invoke
- Context 변경 시 연결된 Tool 정적 재검증
- Evaluation: Golden Case, CLI
- UI: Context, Playground, Tools
- Docker 배포: 컨테이너와 데이터 볼륨

### v0.1에서 하지 않음

데이터베이스 연결(Application DB와 Sidewit 자체 DB 모두), SQL 실행, EXPLAIN, 여러 인스턴스의 동시 쓰기, Generation 영구 저장, Tool 자동 선택(`/resolve`), Vector, RAG, MCP, API Operation, Composite Operation, 여러 parameter style, optional/default/array 파라미터, Operation 파라미터의 Tool 입력 자동 일반화, Tool 버전 이력, 멀티 테넌시, 함수 허용 목록, PostgreSQL 이외 dialect 공식 지원.

---

## 2. 용어

| 용어 | 정의 |
|---|---|
| **Context** | 하나의 데이터 소스를 설명하는 메타데이터. resources, fields, relations, rules, metrics를 담는다. 버전이 있다. |
| **Resource** | 테이블 또는 뷰. |
| **Field** | Resource의 컬럼. |
| **Relation** | Field 사이의 참조 관계(FK 등). |
| **Rule** | 자연어로 쓴 Business Rule. 생성 프롬프트에 들어간다. 선택적 `condition`만 정적 검사에 쓰인다. |
| **Operation** | 실행 계획. v0.1에는 `type: "sql"` 하나만 있다. |
| **Generation** | `/generate` 호출 1회의 결과. 메모리에만 최근 기록을 보관한다(§3.4). |
| **Tool** | 이름, 설명, `input_schema`, Operation 템플릿을 가진 재사용 인터페이스. |
| **Golden Case** | 평가용 질문과 기대 결과 쌍. |

---

## 3. 도메인 모델

### 3.1 Context

Context 파일(§11)의 내용:

```json
{
  "id": "uuid",
  "name": "ecommerce",
  "dialect": "postgresql",
  "timezone": "Asia/Seoul",
  "allow_generated_sql": true,
  "version": 1,
  "created_at": "2026-10-11T00:00:00Z",
  "updated_at": "2026-10-11T00:00:00Z",
  "definition": { "...": "아래" }
}
```

- `name`은 Context 사이에서 유일하다. `dialect`의 v0.1 허용값은 `"postgresql"`이다. `timezone`은 IANA 이름이며 상대 날짜 계산 기준이다(기본 `UTC`).

`definition` 구조:

```json
{
  "resources": [
    {
      "name": "orders",
      "description": "주문",
      "fields": [
        { "name": "id", "type": "bigint", "primary_key": true, "nullable": false, "description": null },
        { "name": "customer_id", "type": "bigint", "nullable": false },
        { "name": "amount", "type": "numeric(12,2)", "description": "실제 결제 금액" },
        { "name": "status", "type": "text", "allowed_values": ["COMPLETED", "CANCELLED", "PENDING"] },
        { "name": "created_at", "type": "timestamptz" }
      ]
    }
  ],
  "relations": [
    { "from": "orders.customer_id", "to": "customers.id" }
  ],
  "rules": [
    { "id": "rule_1", "text": "취소 주문은 매출에서 제외한다.",
      "condition": { "field": "orders.status", "op": "!=", "value": "CANCELLED" } },
    { "id": "rule_2", "text": "매출 = SUM(orders.amount)" }
  ],
  "metrics": []
}
```

규칙:
- Rule의 `condition`(선택)은 그 Rule이 정하는 **업무 상수를 데이터로** 적는다: `{field, op, value}`. 지표 `fixed_filters`와 같은 형태(`FixedCondition`)다. `text`는 모델이 읽는 설명이고, 서버는 문장을 해석하지 않고 `condition`만 쓴다. Rule `condition`과 지표 `fixed_filters`를 합쳐 **등록된 상수**라 하며(`registered_constants`), 생성 SQL은 이를 literal로만 써야 한다(§7 `constant_as_parameter`). 생성 SQL이 그 조건을 **빠뜨렸는지는 검사하지 않는다.** field가 없거나, 값이 field 타입·`allowed_values`와 맞지 않거나, 빈 문자열이면 422 `invalid_rule_condition`이다. condition이 없는 Rule은 프롬프트 지시로만 반영된다.
- `allowed_values`는 **enum 성격의 field에만** 쓰는 그 field의 전체 값 목록이다(예: status, region, grade). 저장된 표기 그대로 적는다. 숫자·날짜·id·자유 텍스트 field는 비워둔다. 목록이 있으면 생성 SQL과 Tool 입력은 그중 하나를 정확히 써야 한다(§7 `value_not_allowed`). 값 중복이나 field 타입과 맞지 않는 값은 422 `invalid_allowed_values`다. DDL import가 채우는 규칙은 §5.1이다.
- 사용자 정의 타입(enum) 이름은 스키마와 따옴표 대소문자를 보존해 비교한다. `public.mood`와 `mood`는 public 기본 스키마에서 같은 타입이며, 서로 다른 스키마의 같은 이름은 합치지 않는다. 임의 `SET search_path`의 상태는 재현하지 않는다.
- 이름은 데이터베이스가 저장하는 형태로 둔다. DDL import는 dialect 규칙을 따른다(PostgreSQL: 따옴표 없는 식별자는 소문자로 접고, 따옴표 식별자는 그대로). 서버가 만드는 SQL(지표 계약)은 꼭 필요할 때만(대소문자 보존·예약어·특수 문자) 따옴표를 쓴다.
- Context 안의 참조(relation, condition, 지표) 이름 비교는 대소문자를 구분하지 않는다. 같은 Context 안에서 resource 이름은 유일하고, 한 resource 안에서 field 이름은 유일하다. SQL 식별자 해석은 §7을 따른다.
- v0.1은 스키마 접두어 없이 resource 이름만 쓴다(`public.orders` 미지원).
- `relations`의 양쪽 끝은 존재하는 field여야 한다. 아니면 저장할 때 422를 반환한다. relation은 `from`(참조하는 쪽)과 `to`(참조되는 쪽)만 가진다. cardinality는 모델링하지 않는다.
- `allow_generated_sql`(기본 `true`)은 맞는 등록 지표가 없을 때 모델이 SQL을 쓰도록 허용하는지를 정한다(§3.1.1). 수정(PUT)할 때는 필수 필드다.
- `name`, `dialect`, `timezone`, `allow_generated_sql`, `definition` 중 하나라도 바뀌면 `version`을 1 올린다.
- 크기 상한: definition은 UTF-8 JSON 128 KiB, resource 100개, field 합계 1,000개.

### 3.1.1 선택적 SQL 지표 계약

`definition.metrics`는 기본 `[]`인 개발자 등록 계약이다. 지표 ID, aggregate, 명시적 INNER JOIN, GROUP BY, 고정 scalar 비교, required inputs, 허용 날짜 컬럼/default를 등록한다. `row_count`는 COUNT(*), `non_null_count`는 명시한 컬럼의 NULL 제외 count다. 고정 조건과 caller input의 field가 겹치거나 타입/참조/JOIN이 잘못되면 Context 저장 시 422다. DDL import는 계약을 보존하고 무효 참조를 warning으로 알린다.

Generate는 **등록 지표를 먼저** 시도한다. 경로는 세 가지다.
- 지표가 없으면 모델이 SQL을 쓴다(`generate_v7`).
- 지표가 있고 `allow_generated_sql: true`(기본)이면 **hybrid**(`hybrid_v2`)로, LLM 호출 한 번에 모델이 경로를 고른다. 등록 지표 하나가 질문 전체에 답하면 `route: metric`(서버가 계약으로 SQL 조립), 아니면 `route: sql`(모델이 SQL 작성)이다. SQL 경로에서도 등록 지표의 집계·고정 조건을 업무 정의로 따르도록 지시한다.
- `allow_generated_sql: false`이면 지표로만 답하고(`metric_plan_v2`), 맞는 지표가 없으면 unanswerable이다. 이 설정은 등록 지표가 1개 이상 필요하다(아니면 422 `no_generation_path`).

성공 응답의 `source`는 `metric`(계약으로 조립) 또는 `generated_sql`(모델 작성)이다. 계약으로 조립한 SQL은 결정론적이지만, **모델이 맞는 지표·입력·기간을 골랐는지는 보장하지 않는다**(예: 허용 값이 `京都`인데 `교토`를 추출하는 실패). 상세 필드와 시간 경계는 [지표 계약](METRIC_CONTRACTS.md)을 따른다.

### 3.2 Operation (SQL)

```json
{
  "type": "sql",
  "dialect": "postgresql",
  "statement": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.region = :region AND o.created_at >= :start_date AND o.status = 'COMPLETED' GROUP BY c.region",
  "parameters": { "region": "서울", "start_date": "2026-09-02" },
  "parameter_types": { "region": "string", "start_date": "date" }
}
```

- `parameter_types`에 쓸 수 있는 값은 `string`, `integer`, `number`, `boolean`, `date`, `date-time` 여섯 가지다.
- `parameters`와 `parameter_types`의 키 집합, 그리고 statement 안 placeholder 집합은 정확히 같아야 한다.
- Python v0.1의 Operation 타입은 `SqlOperation` 하나다. SQL 외 Operation은 구현하지 않았으며, 추가하려면 도메인·API·생성·Tool·UI·평가 계층을 함께 바꿔야 한다.

### 3.3 Tool

Tool 파일(§11)의 내용:

```json
{
  "id": "uuid",
  "context_id": "uuid",
  "name": "regional_revenue",
  "description": "특정 지역의 기간 매출 조회",
  "input_schema": { "...": "§4.1" },
  "operation": { "type": "sql", "statement": "SELECT ... :region ..." },
  "version": 3,
  "context_version": 4,
  "status": "valid",
  "validation_errors": [],
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["..."] },
  "source_generation_id": "uuid | null",
  "metric_binding": { "metric_id": "...", "time_field": "..." },
  "created_at": "...",
  "updated_at": "..."
}
```

- `name`은 `^[a-z][a-z0-9_]{0,63}$`이며 전역 유일하다.
- `status`는 `valid | invalid | stale`이다. `context_version`은 마지막으로 valid 판정을 받은 Context 버전이다.
- `validation`은 마지막 검증 결과다. 비차단 경고를 보존해 조회와 invoke 응답에 전달한다.
- Tool을 수정하면 `version`을 1 올린다. 이력은 저장하지 않는다.
- Tool의 `operation`에는 값이 없다. statement 템플릿만 있고, `dialect`는 Context에서 가져온다.
- `source_generation_id`는 출처 참조일 뿐이며, 그 Generation이 메모리에서 사라진 뒤에도 Tool은 유지된다.
- `metric_binding`은 지표 계약으로 만든 Tool에만 있다(§5.4).

### 3.4 Generation

Generation은 **영구 저장하지 않는다.** 실행 중인 프로세스가 최근 기록을 메모리에 보관한다(기본 200개, `SIDEWIT_GENERATION_HISTORY`). 재시작하면 사라진다. Tool 초안 만들기, 피드백, 거부된 생성 디버깅에 쓰인다.

```
id, context_id, context_version, question, reference_time,
status            -- "ok" | "ambiguous" | "unanswerable" | "failed"
operation, reason, clarifications, tool_suggestion, validation, metric_binding,
provider, model, prompt_version, attempts, latency_ms, input_tokens, output_tokens,
feedback          -- "up" | "down" | null
created_at
```

- `failed`는 내부 상태다. 구조 검증을 끝내 통과하지 못했거나(API 422) LLM 호출이 실패한 경우(API 502)이며, 원인은 `reason`에 남긴다.
- 생성이 끝날 때마다 표준 출력에 한 줄 로그를 남긴다. 담는 것은 메타데이터뿐이다: id, context_id, context_version, status, provider, model, prompt_version, attempts, latency_ms, 토큰 수. **질문, SQL, 파라미터 값은 로그에 남기지 않는다.**
- 질문·Context 내용은 생성 시 LLM Provider로 전송된다. Provider 쪽의 보관 정책은 Sidewit이 통제하지 않는다.

---

## 4. 파라미터 계약

1. **Canonical style은 named `:name` 하나다.** placeholder 이름은 `^[a-z][a-z0-9_]{0,63}$`를 따른다. `?`, `$1`, `%(x)s`는 `unsupported_placeholder_style` 에러로 처리한다. 드라이버 형식 변환은 Application이 맡으며, 값을 SQL 문자열에 직접 끼워 넣으면 안 된다.
2. **파라미터는 사용자 요청에서 나온 가변 값만 나타낸다. Context나 Business Rule이 요구하는 상수는 파라미터가 아니다.** 예를 들어 질문의 "서울"은 `:region`이 되지만, Rule이 요구하는 `'COMPLETED'`는 리터럴로 남는다.
3. **Generate는 상대 시간을 절대값으로 바꾼다.** "최근 30일", "지난달", "어제" 같은 표현은 `reference_time`과 Context `timezone`을 기준으로 절대값 파라미터가 된다. 생성 SQL에는 `NOW()`나 `CURRENT_DATE`를 쓰지 않는다.
   - `reference_time`은 상대 시간 표현을 재현 가능하게 만드는 기준일 뿐이다. 어떤 날짜 컬럼으로 기간을 잡을지는 Context(field description, Business Rule)를 보고 정한다(§6.3).
   - 비교 대상 컬럼이 timestamp 계열이면 `date-time`(오프셋 포함), date 타입이면 `date`를 쓴다.
4. **Operation 파라미터와 Tool 입력은 개념적으로 다르다.** Operation 파라미터는 이번 요청을 위한 구체값이고, Tool 입력은 재사용을 위한 계약이다.
   - `/tools/draft`는 Operation 파라미터를 **그대로** Tool 입력으로 옮긴다. 자동으로 일반화하지 않는다.
   - 일반화는 사용자가 Tool Editor에서 한다. 예: `:start_date`를 `NOW() - :days * INTERVAL '1 day'`로 바꾸고 입력을 `days: integer`로 바꾼다. Tool statement에서는 `NOW()`를 허용한다.
5. **날짜와 시간은 ISO 8601 문자열이다.** JSON Schema에서는 `{"type":"string","format":"date"}`, `{"type":"string","format":"date-time"}`로 쓴다. 타입 변환은 Application이 맡는다.
6. **v0.1에서 지원하지 않는 것**: optional 파라미터, default, 배열(IN 바인딩), 문자열 리터럴 안의 placeholder.
7. **Sidewit은 값을 SQL 문자열에 끼워 넣지 않는다.** statement는 placeholder를 그대로 둔 채 반환하고, 값은 `parameters`로 따로 넘긴다.

### 4.1 Tool `input_schema` 제약

```json
{
  "type": "object",
  "properties": {
    "region":     { "type": "string", "description": "조회할 지역" },
    "start_date": { "type": "string", "format": "date", "description": "조회 시작일" }
  },
  "required": ["region", "start_date"],
  "additionalProperties": false
}
```

- 최상위는 `type: object`이고 `additionalProperties: false`여야 한다.
- 각 property의 `type`은 `string`, `integer`, `number`, `boolean` 중 하나다. `string`에만 `format: date | date-time`을 쓸 수 있다.
- property에 허용되는 키워드는 `type`, `format`, `description`, `enum`(string 전용)뿐이다. `default`는 거부한다.
- `required`는 모든 property 키와 같아야 한다(중복 없음). property 이름은 placeholder 이름 규칙을 따른다.
- 위 제약을 어기면 `invalid_input_schema` 에러이고, `ref`에 위치(`properties.days.default` 등)를 담는다.
- `parameter_types`와는 이렇게 대응한다: `date`는 `string`+`format: date`, `date-time`은 `string`+`format: date-time`, 나머지는 같은 이름의 타입.
- `enum`은 입력 범위만 제한한다. SQL이 그 값을 어떻게 쓰는지는 §7 검사가 다룬다.

---

## 5. API

공통 사항:
- Base path는 `/api/v1`이다.
- `SIDEWIT_API_KEY` 환경변수가 설정되어 있으면 `/health`를 제외한 모든 요청에 `Authorization: Bearer <key>`가 있어야 한다. 없거나 틀리면 401 `unauthorized`다. 설정되어 있지 않으면 인증 없이 동작한다. key 하나가 관리·생성·invoke를 모두 허용하므로, 최종 사용자에게 직접 주지 않는다.
- `GET /health`는 인증 없이 호출할 수 있다. 데이터 폴더를 읽고 쓸 수 있으면 200, 아니면 503이다.
- 요청 형식 오류(Pydantic 검증 실패)는 422 `invalid_request`, 없는 경로는 404 `not_found`다.
- SQL/DDL 복잡도: 문자열·주석을 제외한 괄호/대괄호 중첩 32단계, 토큰 20,000개, AST 깊이 64단계·노드 10,000개까지 허용한다. 초과는 SQL 검증의 `sql_parse_error`, DDL의 422 `ddl_parse_error`로 거부하며, 추가 분석 중 재귀 한계도 `sql_analysis_error`로 처리한다.
- 상한: HTTP 본문 1 MiB(초과 413 `request_too_large`), 질문 8,000자, DDL 256,000자, SQL 64,000자, Context 크기는 §3.1. 모델 입력 상한 초과는 422다.
- 에러 형식:

```json
{ "error": "tool_invalid", "message": "Tool is incompatible with the current context.", "details": { } }
```

### 5.1 Context

| Method | Path | 설명 |
|---|---|---|
| GET | `/contexts` | 목록 |
| POST | `/contexts` | 생성 `{name, dialect, timezone?, allow_generated_sql?, definition?}` |
| GET | `/contexts/{id}` | 조회 |
| PUT | `/contexts/{id}` | 수정 `{name, dialect, timezone, allow_generated_sql, definition, expected_version}` |
| DELETE | `/contexts/{id}` | 삭제. 연결된 Tool이 있으면 409 `context_has_tools` |
| POST | `/contexts/{id}/import-ddl` | DDL 파싱 미리보기 `{ddl, mode: "merge" \| "replace"}`. 저장하지 않음 |

`PUT`:
- `expected_version`이 현재 버전과 다르면 409 `version_conflict`다(낙관적 잠금). 이름이 다른 Context와 겹치면 409다.
- 성공하면 연결된 Tool을 재검증한 결과를 함께 반환한다(§5.2).

```json
{
  "context": { "...": "..." },
  "revalidation": {
    "checked": 3,
    "valid": ["regional_revenue", "monthly_sales"],
    "invalid": [{ "name": "customer_orders", "errors": [{ "code": "unknown_field", "message": "...", "ref": "orders.amount" }] }]
  }
}
```

`import-ddl`:
- 요청의 선택적 `expected_version`이 현재 Context 버전과 다르면 409 `version_conflict`다. 응답의 `context_version`은 미리보기의 기준 버전이며, 적용 후 PUT의 `expected_version`으로 사용한다. 다른 버전의 편집 내용과 합치지 않는다. UI는 Context 버전·DDL·모드가 달라지거나 요청이 진행/실패 중이면 이전 미리보기 적용을 막는다.
- 지정한 dialect로 `CREATE TABLE`을 파싱해 resources, fields, PK, nullable을 채우고, FK가 있으면 relations도 만든다. 지원하지 않는 문장(`CREATE EXTENSION`, 인덱스 등)은 warning과 함께 무시한다. pg_dump의 psql 메타 명령(`\restrict` 등)도 무시한다.
- `allowed_values`는 `CREATE TYPE ... AS ENUM`과 값 목록 CHECK(`col IN (...)`, `col = a OR col = b`, pg_dump 형식 `col = ANY (ARRAY[...])`와 varchar 컬럼의 `(col)::text = ANY (...)`)에서 채운다.
  - literal의 캐스트는 PostgreSQL처럼 계산한다(§7.3, 검증기와 공용): `'1'::integer`는 1, `'ABC'::varchar(1)`은 `'A'`, `1.235::numeric(4,2)`는 1.24. PostgreSQL이 오류를 내는 캐스트(`'1.5'::integer`, 범위 초과)와 지원하지 않는 캐스트는 읽지 않고 warning을 남긴다.
  - 컬럼 쪽 캐스트는 길이 제한 없는 텍스트(`::text`)만 컬럼 자체의 목록으로 본다. `col::varchar(1) IN (...)`은 컬럼을 제한하지 않으므로 가져오지 않는다.
  - 한 컬럼에 ENUM과 CHECK가 여럿이면 모두 동시에 성립하므로 교집합을 쓴다. 교집합이 비거나 목록에 literal이 아닌 값이 있으면 가져오지 않고 warning을 남긴다.
  - pg_dump의 enum CHECK(`m = ANY(ARRAY['ok'::public.mood, ...])`)는 같은 DDL에 선언된 enum label이 확인될 때만 읽는다.
  - 범위 조건(`n > 0`) 같은 값 목록이 아닌 CHECK는 대상이 아니다.
  - 미리보기가 저장 가능한 Context가 되도록, Context 검증(`invalid_allowed_values`)에 걸리는 목록은 가져오지 않고 warning을 남긴다.
  - 정확히 표현할 수 없는 숫자 CHECK 값(§6.2의 `exact_number` 규칙)은 422 `ddl_parse_error`다.
- Context 이름 규칙에 맞지 않는 DDL 식별자는 422 `ddl_parse_error`이며 저장된 Context를 바꾸지 않는다.
- `merge` 모드에서는 이름이 같은 resource와 field의 기존 `description`을 유지한다. `allowed_values`는 DDL이 ENUM/CHECK로 선언하면 DDL 값을, 아니면 기존 값을 쓴다. 새로 생긴 것은 추가하고, DDL에 없는 기존 항목은 유지하되 warning으로 알린다.
- 응답은 `{ "context_version": 1, "definition": {...}, "warnings": ["..."] }`이다. 클라이언트가 확인한 뒤 `PUT`으로 저장한다.

### 5.2 재검증 계약

> Context가 바뀌면 영향받는 Tool은 invoke되기 전에 반드시 재검증되어야 한다.

`PUT /contexts/{id}` 처리 중에 동기로 수행하며, Context 파일과 Tool 파일들을 같은 쓰기 구간에서 갱신한다(§11). 각 Tool의 결과:
- 성공: `status=valid`, `context_version=<새 버전>`, `validation_errors=[]`
- 실패: `status=invalid`, `validation_errors=[...]`

`stale`(Tool의 `context_version`이 현재 Context 버전과 다름)은 재검증 전의 과도 상태다. invoke 시점에 Tool이 `stale`이면 즉시 재검증한 뒤 처리한다.

### 5.3 Generate

`POST /generate`

```json
{
  "context_id": "uuid",
  "question": "지난 30일 서울 매출 알려줘",
  "reference_time": "2026-10-02T09:00:00+09:00"
}
```

`metric_id`, `date_field`, `period`, `require_metric`은 optional 지표 옵션이다. 하나라도 주면 그 요청은 지표로만 답한다. `require_metric: true`는 지표를 고르지 않고 이 제한만 건다. date_field는 metric_id와 그 계약의 허용 field가 필요하다. period는 rolling_days(days 1~3660), previous_day/week/month(days=null)만 지원하며 서버가 계산한다. 잘못된 옵션은 LLM 호출 전 422 `invalid_metric_request`다.

`reference_time`을 생략하면 서버의 현재 시각을 Context `timezone`으로 변환해 쓴다.

응답 200은 세 가지 중 하나다.

```json
{
  "generation_id": "uuid",
  "context_version": 4,
  "status": "ok",
  "source": "generated_sql",
  "operation": {
    "type": "sql",
    "dialect": "postgresql",
    "statement": "SELECT ... WHERE c.region = :region AND o.created_at >= :start_date AND o.status = 'COMPLETED' ...",
    "parameters": { "region": "서울", "start_date": "2026-09-02" },
    "parameter_types": { "region": "string", "start_date": "date" }
  },
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["syntax", "statement_policy", "resources", "fields", "parameters", "placeholder_safety"] },
  "tool_suggestion": { "name": "regional_revenue", "description": "특정 지역의 기간 매출 조회" },
  "metric_binding": null
}
```

```json
{ "generation_id": "uuid", "context_version": 4, "status": "ambiguous",
  "reason": "Revenue has multiple possible interpretations.",
  "clarifications": ["Should revenue be based on completed payments or placed orders?"] }
```

```json
{ "generation_id": "uuid", "context_version": 4, "status": "unanswerable",
  "reason": "The current context does not contain data required to calculate churn rate." }
```

처리 흐름:
1. Planner를 고른다(`generation/planners.py`): 지표가 없으면 `SqlPlanner`(`generate_v7`), 지표가 있고 생성 SQL을 허용하면 `HybridPlanner`(`hybrid_v2`), 허용하지 않거나 요청이 지표 옵션을 주면 `MetricPlanner`(`metric_plan_v2`). Hybrid는 모델의 `route`에 따라 SQL/Metric planner의 변환·검증을 재사용하고, `route`가 없으면 `route_missing`으로 repair한다. 지표 옵션을 지표 없는 Context에 보내면 LLM 호출 전 422 `invalid_metric_request`다. 지표 경로는 서버 compiler가 aggregate/JOIN/filter/group을 구성하고 날짜 기준을 구조화 옵션 → 유일한 명시 field → default 순서로 선택한다. 필요한 기준이 없으면 ambiguous다.
2. `status=ok`이면 정적 Validator를 Generate 정책으로 실행한다(§7). SQL 경로는 `generate_policy(question)`(요청값 literal 검사 포함), 지표 경로는 `CONTRACT_POLICY`다. `ambiguous`/`unanswerable`이면 검증하지 않고 반환한다. LLM이 SQL을 함께 보내더라도 버린다.
3. 검증에 실패하면 에러 목록을 붙여 **한 번만** 다시 요청한다(repair).
4. 그래도 실패하면 422 `generation_invalid`다. **응답에는 SQL을 넣지 않는다.** 생성된 SQL은 메모리의 Generation 기록에만 남는다.
   ```json
   { "error": "generation_invalid", "message": "Generation failed validation.", "details": { "generation_id": "uuid", "validation_errors": [{ "code": "unknown_field", "message": "...", "ref": "orders.amt" }] } }
   ```
5. LLM 호출이 실패하면 502 `llm_error`다. `details`에는 `generation_id`만 담는다. provider 원본 에러는 자격 증명 일부를 포함할 수 있으므로 공개 응답과 로그에 넣지 않고, 메모리 기록의 `reason`에만 마스킹해 남긴다. 마스킹 대상은 설정된 key 값, `sk-…`, `AIza…`, `Bearer …`, `key=…` 형태다. 원본 예외는 체인(`__cause__`)으로도 남기지 않는다. 설정의 API key 값은 `SecretStr`로 보관한다.
6. LLM provider가 설정되지 않았으면 503 `llm_not_configured`이며 Generation을 만들지 않는다.
7. 전체 처리에는 90초 deadline을 적용한다. Provider 호출 timeout은 60초이고 최초 호출과 repair가 이 deadline을 공유한다. 초과하면 504 `generation_timeout`이며 Generation 기록이 남지 않을 수 있다. nginx는 100초, UI 요청은 110초까지 기다린다.
8. LLM 응답을 기다리는 동안 Context를 잠그지 않는다. 그 사이 Context가 삭제되면 404 `context_not_found`다.

`POST /generations/{id}/feedback` `{ "feedback": "up" | "down" }`
`GET /generations?context_id=&limit=` (메모리의 최근 기록)
`GET /generations/{id}` (디버그용 상세. 실패한 Generation의 SQL과 검증 결과 포함)

> `/generations*`는 관리 UI·개발용이다. 검증에 실패한 SQL과 질문 원문이 보이므로 공개 인터페이스로 쓰지 않는다. 기록이 메모리에서 밀려났거나 재시작으로 사라졌으면 404 `generation_not_found`다.

### 5.4 Tools

| Method | Path | 설명 |
|---|---|---|
| GET | `/tools?context_id=` | 목록(status 포함) |
| GET | `/tools/{name}` | 조회 |
| POST | `/tools/draft` | Generation에서 초안 생성 `{generation_id}`. 저장하지 않음 |
| POST | `/tools` | 생성 `{context_id, name, description, input_schema, operation, source_generation_id?}`. 검증 실패 시 422 `tool_validation_failed`(`details.validation_errors`, `details.warnings`). 이름이 겹치면 409 `tool_name_conflict` |
| PUT | `/tools/{name}` | 수정 `{expected_version, description, input_schema, operation}`. 버전이 다르면 409 `version_conflict`. name과 context는 바꿀 수 없다. 내용이 바뀌면 version+1. 검증에 실패하면 422이고 Tool은 그대로 |
| DELETE | `/tools/{name}` | 삭제 |
| POST | `/validate` | 저장하지 않는 검증(에디터용) `{context_id, operation, input_schema?}`. `input_schema`가 있으면 Tool 검증(§7.5)이며, `operation.parameter_types`/`parameters`를 함께 보내면 422 `invalid_validate_request`다. 없으면 `operation.parameter_types`/`parameters`로 SQL 검증. 항상 `DEFAULT_POLICY`이며 Generate 호환성 검사가 아니다 |
| POST | `/tools/{name}/invoke` | 결정론적 호출 `{arguments, expected_version?}` |
| POST | `/tools/{name}/refresh-contract` | `{expected_version}` 필수(충돌 409). 지표에 묶인 Tool의 SQL과 입력을 현재 계약으로 다시 만든다. 입력 description/enum은 같은 타입이면 유지한다. 바뀌면 version+1. 묶이지 않았거나 지표/날짜 binding을 현재 계약에서 쓸 수 없으면 409 `metric_contract_unavailable` |

`POST /tools/draft`는 LLM을 호출하지 않는다.
- 메모리에 있는 `status=ok` Generation에만 쓸 수 있다. 없으면 404 `generation_not_found`, 다른 상태면 409 `generation_not_ok`.
- 초안은 재료일 뿐이고 검증하지 않는다. 저장 시점의 Context로 다시 검증한다.
- `operation.parameter_types`로 `input_schema`를 1:1로 만든다. name과 description은 `tool_suggestion`으로 채운다. 문자열 입력이 허용 값이 있는 field와 비교되면 그 값을 `enum`으로 채운다.
- 응답에 `example_arguments`(Generation의 parameters)를 포함한다.

```json
{
  "context_id": "uuid",
  "name": "regional_revenue",
  "description": "특정 지역의 기간 매출 조회",
  "input_schema": { "type": "object", "properties": { "...": {} }, "required": ["region", "start_date"], "additionalProperties": false },
  "operation": { "type": "sql", "statement": "SELECT ... :region ... :start_date ..." },
  "example_arguments": { "region": "서울", "start_date": "2026-09-02" },
  "source_generation_id": "uuid",
  "metric_binding": null
}
```

`POST /tools`의 `source_generation_id`:
- 같은 Context의 `ok` Generation이 메모리에 있어야 한다. 아니면 422 `invalid_source_generation`.
- 지표로 만든 Generation이면 그 `metric_binding`을 상속한다. 요청에서 생략·null로 보내도 제거되지 않고 다른 binding으로 바꿀 수 없다. PUT은 binding을 바꾸지 않는다.

`POST /tools/{name}/invoke`

```json
{ "arguments": { "region": "서울", "start_date": "2026-09-02" }, "expected_version": 3 }
```

```json
{
  "tool": "regional_revenue",
  "tool_version": 3,
  "context_version": 4,
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["..."] },
  "operation": {
    "type": "sql",
    "dialect": "postgresql",
    "statement": "SELECT ... WHERE c.region = :region AND o.created_at >= :start_date ...",
    "parameters": { "region": "서울", "start_date": "2026-09-02" },
    "parameter_types": { "region": "string", "start_date": "date" }
  }
}
```

처리 흐름: Tool 조회 → `expected_version`이 있으면 버전 확인(다르면 409 `version_conflict`) → (`valid`가 아니거나 `context_version`이 현재 Context 버전과 다르면 재검증해 결과를 기록) → `invalid`이면 409 `tool_invalid` → `arguments`를 `input_schema`와, 입력이 비교되는 field의 `allowed_values`로 검증(실패 시 422 `invalid_arguments`) → Operation 조립. 입력별 허용 값은 (Tool 버전, Context 버전)마다 한 번 계산해 재사용한다. LLM 호출, 문자열 치환, SQL 실행은 없다. `parameters`와 `parameter_types`의 키 순서는 `input_schema.required` 순서를 따른다.

에러 응답:
- 404 `tool_not_found`
- 409 `tool_invalid`(`details.validation_errors`), 409 `version_conflict`
- 422 `invalid_arguments`(`details.errors`의 `{code, message, ref}`): `missing_argument`, `unexpected_argument`, `argument_type_mismatch`(date/date-time 형식 포함), `argument_not_in_enum`, `value_not_allowed`(현재 Context의 허용값 위반)
- 요청 본문의 최상위 키는 `arguments`, `expected_version`뿐이다. 다른 키는 422 `invalid_request`

Tool과 Context 제약:
- Tool의 JSON Schema enum은 Context 제약을 완화할 수 없다. 저장·수정·재검증은 enum이 해당 field의 `allowed_values` 안에 있는지 확인하고, 충돌하면 `input_enum_conflict`다. 같은 파라미터를 여러 제한 field와 비교하면 허용값의 교집합을 쓴다.
- enum 없는 수동 Tool도 invoke 때 현재 Context의 허용값을 검사한다. 숫자·boolean 입력에도 적용한다.
- 잘못된 `input_schema` 형태는 서버 예외가 아니다. `/validate`는 `valid: false`와 `invalid_input_schema`, 저장은 422 `tool_validation_failed`다.

지표 계약에 묶인 Tool:
- 현재 계약 template과 statement AST·parameter types가 같아야 한다. 공백·주석과, 같은 이름으로 해석되는 따옴표 차이(`"orders"`와 `orders`)만 허용한다. 다르면 `metric_contract_mismatch`다.
- 계약은 Context 안에 있으므로 Context 변경 시 재검증한다. incompatible Tool은 409이며 `refresh-contract`로 현재 계약에 맞출 수 있다.
- 묶인 Tool의 SQL과 입력 이름·타입은 계약 소유라 UI에서 읽기 전용이다. binding 없는 Tool은 일반 정적 검증만 받는다.

---

## 6. LLM 생성 계약

### 6.1 Provider 추상화

```python
class LLMProvider(Protocol):
    name: str
    model: str
    async def generate_structured(self, system: str, messages: list[Message], schema: type[BaseModel]) -> StructuredResult: ...
    # StructuredResult: parsed(BaseModel), input_tokens, output_tokens
```

- 구현체는 `OpenAIProvider`, `GeminiProvider`, 테스트용 `FakeProvider`(미리 정한 응답 반환)다.
- 설정 환경변수: `SIDEWIT_LLM_PROVIDER`, `SIDEWIT_LLM_MODEL`, `OPENAI_API_KEY`, `GEMINI_API_KEY`.
- validator 이후 코드는 provider에 의존하지 않는다.
- 실행해 확인한 설정은 OpenAI `gpt-6-luna`, Gemini `gemini-3.8-flash`와 `gemini-3.1-flash-lite`다. 두 Gemini 모델은 adapter가 thinking `low`를 설정한다. Provider 요청 timeout은 60초이며 SDK 자동 재시도는 쓰지 않는다. 다른 모델은 평가 suite(§9)로 확인한다.

### 6.2 LLM 출력 스키마

provider 호환성을 위해 동적 키 객체 대신 리스트를 쓴다. `value`는 provider마다 다른 union 타입 지원을 피하려고 항상 문자열로 받고, 서버가 `type`에 맞게 변환한다.

```json
{
  "status": "ok | ambiguous | unanswerable",
  "reason": "string | null",
  "clarifications": ["string"],
  "statement": "string | null",
  "parameters": [{ "name": "region", "type": "string", "value": "서울" }, { "name": "days", "type": "integer", "value": "30" }],
  "tool_suggestion": { "name": "snake_case", "description": "string" }
}
```

- integer/number는 **정확히 표현될 때만** 숫자로 바꾼다(`domain/params.exact_number`, 지표 경로와 DDL CHECK 값도 같은 규칙): bigint 범위의 정수 값은 int(`"9007199254740993"`, `"3.0"`), 그 밖은 float로 정확할 때만(`"0.1"`, `"1e30"`). 반올림·무한대가 되는 값과 지수가 지나치게 큰 값은 문자열로 남아 `parameter_type_mismatch`로 repair된다. 서버는 숫자를 조용히 반올림하지 않는다.
- 같은 이름의 파라미터가 두 번 이상 오면 어느 쪽도 고르지 않고 `duplicate_parameter`로 repair를 요청한다(Generate 변환 단계 전용 코드).
- `request_value_literal`도 Generate 전용 코드다. `generate_policy(question)`로 실행되며(`validation/request_literals.py`), 비교/IN/BETWEEN/LIMIT/OFFSET의 직접 literal 값이 요청 원문에 그대로 나오면 repair로 보낸다. 날짜·기간 단위가 바로 붙은 숫자(`1월`, `30일`, `7 days`)는 기간 표현이라 일치로 보지 않는다. 등록된 상수와 같은 field·값의 literal은 검사하지 않는다. condition이 없는 Rule을 위한 대체 수단으로, Context의 description이나 Rule 문장에 값이 나와도 상수일 가능성으로 보고 보호한다. 문장 의미나 Rule 적용을 검증하는 것은 아니며, 수동 Tool/`DEFAULT_POLICY`에는 적용하지 않는다.
- 이 DTO는 `generation/` 안에서만 쓴다. Domain 모델과 공개 API로 노출하지 않는다.

### 6.2.1 등록 지표의 내부 출력

```json
{
  "status": "ok",
  "reason": null,
  "clarifications": [],
  "metric_id": "regional_revenue",
  "parameters": [{"name":"region","type":"string","value":"서울"}],
  "period": {"kind":"rolling_days","days":30},
  "tool_suggestion": null
}
```

이 DTO에는 statement, 집계 함수, 날짜 컬럼, JOIN, 고정 조건이 없다. 모든 provider 필드는 required이고 nullable은 명시적 null이다. 지표·입력 이름과 타입, 기간을 서버가 검사한 뒤 compiler가 canonical `:name` SQL을 만들고 `CONTRACT_POLICY`로 검증한다. 실패 시 한 번의 repair와 422/no-SQL 계약을 따른다. 모델이 지표나 추출 값을 잘못 고른 의미 오류는 검증하지 않는다.

### 6.3 프롬프트에 반드시 포함할 것

- dialect, `reference_time`, timezone
- Context 렌더링(resources/fields와 타입·설명·허용 값, relations, rules, 지표)
- 규칙:
  - Context에 있는 resource와 field만 사용한다.
  - 기간 조건에 쓸 날짜 컬럼은 field description과 Business Rule로 고른다. 후보가 여럿인데 결정할 수 없으면 `ambiguous`와 후보 컬럼을 반환한다.
  - 필요한 데이터가 Context에 없으면 `unanswerable`을 반환한다.
  - 해석이 둘 이상 가능하고 Rule로 결정되지 않으면 `ambiguous`와 clarifications를 반환한다.
  - 하나의 SELECT 문만 생성한다.
  - §4의 2·3번 규칙(가변 값만 파라미터로, 상대 시간은 절대값으로, `NOW()` 금지, 리터럴 안 placeholder 금지).
  - Business Rule을 반영한다.
- 현재 프롬프트는 `sidewit/prompts/generate_v7.md`, `sidewit/prompts/hybrid_v2.md`, `sidewit/prompts/metric_plan_v2.md`이며 `prompt_version`을 Generation에 기록한다. field의 `allowed_values`는 '정확한 표기로만, 번역·음역·창작 금지'로 지시한다. 설명 없는 날짜·타임스탬프 field가 여럿이면 기간 선택이 모호하다는 힌트를 렌더링하되, `updated_at`·`deleted_at` 같은 bookkeeping timestamp는 세지 않는다. 이는 모델에 주는 지시이며 강제 검증이 아니다.

---

## 7. 정적 SQL Validator

순수 함수로 구현한다(DB와 LLM 의존 없음).

```python
def validate_sql_operation(
    statement: str,
    dialect: str,
    context: ContextDefinition,
    declared_params: dict[str, ParamType],
    values: dict[str, Any] | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
) -> ValidationResult
```

```json
{ "valid": false, "errors": [{ "code": "...", "message": "...", "ref": "orders.amount" }], "warnings": [], "checks": ["..."] }
```

### 7.1 보장 범위

이 Validator가 확인하는 것: SQL 파싱, 문장 하나·SELECT 계열, 읽는 테이블이 모두 Context resource인지, 이름 해석(§7.4), 파라미터 계약, 상대 시간(정책별), 지원 표현식 안에서의 허용 값과 등록 상수 정책.

확인하지 않는 것: 함수·연산자의 존재와 인자 타입, 타입 호환성, 실제 DB에서의 실행 가능성, 권한, 실행 비용, 함수 부작용, Business Rule 준수, 질문에 맞는 답인지. 이런 오류는 Application이 SQL을 실행하거나 자기 DB에서 EXPLAIN할 때 드러난다. UI는 "✓ SQL structurally validated"로 표기하며 "Context validated"나 "safe"라고 쓰지 않는다.

**지원 범위는 넓히지 않는다.** 필수 정책(허용 값, 등록 상수)을 지원 표현식 밖이라 분석할 수 없으면 통과시키지 않고 `value_unverifiable`로 거부한다.

### 7.2 정책과 검사 목록

`ValidationPolicy`는 경로별 정책이다.

| 정책 | 사용하는 곳 | `allow_relative_time` | `request_text` | `allow_constant_parameters` |
|---|---|---|---|---|
| `generate_policy(question)` | `/generate` SQL 경로(최초 생성과 repair) | `false` | 질문 | `false` |
| `GENERATE_POLICY` | 평가기의 모델 작성 SQL 재검증 | `false` | 없음 | `false` |
| `CONTRACT_POLICY` | `/generate` 지표 경로, 평가기의 지표 결과 재검증 | `false` | 없음 | `true` |
| `DEFAULT_POLICY` | 일반 SQL 검증, Tool 저장·재검증, `/validate` | `true` | 없음 | `true` |

지표 경로의 파라미터는 계약이 선언한 입력이므로, 다른 지표의 고정 조건과 같은 field라도 `constant_as_parameter` 대상이 아니다.

| 검사 | code | 수준 |
|---|---|---|
| sqlglot 파싱 실패, SQL 64,000자 초과 또는 복잡도 제한 초과 | `sql_parse_error` | error |
| 문장이 2개 이상 | `multiple_statements` | error |
| SELECT / WITH…SELECT / UNION 이외의 문장, `SELECT INTO`, `FOR UPDATE` | `statement_not_allowed` | error |
| Context에 없는 resource(보이는 CTE 이름 제외), 스키마 접두어 테이블, 시스템 테이블(`pg_roles` 등), 보이지 않는 alias | `unknown_resource` | error |
| Context에 없는 field | `unknown_field` | error |
| 여러 소스에 있는 field를 한정자 없이 사용, 또는 같은 이름의 출력 컬럼이 둘 이상인 소스의 그 컬럼을 참조 | `ambiguous_field` | error |
| 한 FROM 안의 중복 alias, 컬럼 수가 다른 set-operation arm, 길이가 다른 VALUES 행, FROM 없는 `SELECT *` | `sql_analysis_error` | error |
| JOIN 조건이 Context relations에 선언되지 않은 키 쌍 | `undeclared_relation` | warning |
| `?`, `$1`, `%(x)s` 형식 placeholder | `unsupported_placeholder_style` | error |
| 문자열 리터럴 안에 `:name` 패턴 | `placeholder_in_literal` | error |
| statement에는 있는데 선언되지 않은 placeholder | `undeclared_parameter` | error |
| 선언됐는데 statement에 없는 파라미터 | `unused_parameter` | error |
| 값이 선언된 타입과 맞지 않음(값이 있을 때) | `parameter_type_mismatch` | error |
| DB의 현재 시각에 의존하는 표현(`allow_relative_time=false`일 때만) | `relative_time_function` | error |
| 등록된 상수와 **같은 field·연산자·값**을 파라미터로 바인딩함(`allow_constant_parameters=false`, 값이 있을 때만). `:x <= fee`는 `fee >= :x`로 본다 | `constant_as_parameter` | error |
| `allowed_values`가 있는 field와 `=`, `!=`, `IS [NOT] DISTINCT FROM`, `[NOT] IN`, `= ANY/SOME/ALL(ARRAY[...])`로 비교한 literal(또는 값이 주어진 파라미터)이 목록에 없음. 텍스트는 정확히 일치, 숫자는 값으로 비교한다. 범위 비교·LIKE는 대상이 아니다 | `value_not_allowed` | error |
| 허용 값이 있는 field와 비교한 값을 지원 범위 안에서 계산할 수 없음(예: `upper(:s)`) | `value_unverifiable` | error |

`relative_time_function` 대상(PostgreSQL): `NOW()`, `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP`, `LOCALTIME`, `LOCALTIMESTAMP`, `clock_timestamp()`, `statement_timestamp()`, `transaction_timestamp()`, `timeofday()`, 날짜·시간 타입으로 캐스트한 `'now'`·`'today'`·`'tomorrow'`·`'yesterday'`, 인자가 하나인 `age(x)`. 목적은 `reference_time` 재현성뿐이며 `random()` 같은 일반 비결정 함수는 대상이 아니다.

### 7.3 값 정책의 지원 범위

허용 값과 상수 검사는 비교가 SELECT의 어디에 있든 적용한다: WHERE, HAVING, JOIN ON, `FILTER (WHERE ...)`, `CASE WHEN`, 투영, ORDER BY.

값 출처(`validation/sql_values.py`): CTE, derived table, VALUES, 컬럼 별칭, 단일 컬럼 서브쿼리(`(SELECT :s)`, `ANY(SELECT ...)`, `IN (VALUES ...)`), set operation의 출력 출처를 추적한다. literal/parameter와 CAST 목록, DB에서 읽는 값, 출처나 변환을 확인할 수 없는 값을 구분하며, 확인 불가를 DB 값으로 간주해 검사를 생략하지 않는다.

컬럼 쪽 lineage: 직접 column projection은 alias·CTE·derived table·`*`·set operation(모든 arm이 같은 field일 때)을 거쳐도 원본 field의 허용값과 상수 제약을 유지한다. 집계·산술·함수 결과와, arm마다 원본이 다른 set-operation 컬럼은 원본 허용값을 상속하지 않는다. 괄호, `COLLATE`, 길이 제한 없는 텍스트 cast(`::text`, `::varchar`)는 **문자열 field**의 값을 바꾸지 않으므로 같은 field로 본다(boolean `flag::text`는 문자열이 되어 원본 허용 값을 따르지 않는다). 같은 enum으로 캐스트한 컬럼도 출처를 유지한다. boolean field와 비교한 따옴표 literal은 PostgreSQL boolean 입력 규칙(`'true'`, `'t'`, `'yes'`, `'on'`, `'1'` 등)으로 읽는다.

CAST 값 평가(`domain/dialect.cast_value`, DDL import와 공용)는 원본 scalar 범주와 목표 타입·수식어의 조합으로 결정한다. SQL 숫자 literal은 문자열 literal과 구분하며 연속 CAST의 중간 결과 범주를 유지한다. CAST를 제거하지 않고 순서대로 평가한다.
- 문자열 → text/varchar: 문자열 보존, varchar 길이 제한(`'APPLE'::varchar(1)`은 `'A'`).
- 문자열·정수·숫자 → smallint/integer/bigint: 문자열은 부호와 십진 정수 숫자만 허용하고, 숫자는 반올림한 뒤 목표 범위를 검사한다.
- 문자열·정수·숫자 → numeric: 정확히 표현할 수 있는 값에 한해 정밀도·scale·overflow를 검사한다.
- 문자열·boolean → boolean: PostgreSQL boolean 입력 표기의 지원 범위.
- 문자열 → Context field와 같은 enum: 같은 타입에 등록된 문자열 label의 합집합 안에서만 평가하고, 최종 값은 비교 field의 `allowed_values`로 검사한다. enum → text/varchar는 지원하고 enum → 숫자/boolean은 지원하지 않는다. `:m::mood::varchar(2)::mood`에 `okay`를 넣어 잘못된 중간 label을 숨길 수 없다.

char, real/double precision, 위 조건 이외의 사용자 정의 타입, 숫자/boolean → 문자열 등은 평가하지 않는다. 허용값 검증에 이 변환이 필요하면 `value_unverifiable`로 거부한다. 값이 바뀌는 캐스트를 거친 파라미터는 field의 허용 값으로 제한하지 않고(그 결과는 invoke 때 실제 값으로 검사한다), 계산할 수 없는 입력 표현식은 Tool 저장 단계에서 거부한다.

### 7.4 이름 해석

`validation/sql_analysis.py` 한 곳에 있다.
- 식별자는 PostgreSQL의 저장 이름 기준이다. unquoted 이름은 소문자로 정규화하고 quoted 이름은 보존한다. 테이블·컬럼·alias·CTE에 같은 규칙을 적용한다(`"Orders"`는 `orders`와 다르다).
- 각 SELECT에 직접 쓰인 컬럼을 그 SELECT의 FROM/JOIN 소스에 대해 검사한다.
- 가시성은 이름이 쓰인 **위치**로 정한다. FROM은 쉼표로 나뉜 그룹이고 각 그룹은 JOIN 트리다. JOIN은 쉼표보다 강하게 묶이고(`a, b JOIN c ON ...`은 a와 (b JOIN c)), 괄호 JOIN은 중첩된 트리이며, 별칭을 붙인 괄호 JOIN(`(a JOIN b) AS j`)은 하나의 항목이다. 상관 서브쿼리는 바깥 쿼리의 FROM 항목을 모두 본다. LATERAL 항목과 FROM의 테이블 함수 인자는 자기보다 **앞의** 항목을 보되, RIGHT/FULL JOIN의 오른쪽이면 그 JOIN의 왼쪽은 보지 못한다. JOIN의 ON은 같은 그룹에서 그 JOIN까지의 항목만 본다. 한정자 없는 이름은 그 위치의 JOIN 상태로 해석한다. LATERAL이 아닌 derived table과 CTE는 같은 FROM의 다른 항목을 보지 못한다.
- `JOIN ... USING (c)`와 `NATURAL JOIN`은 공통 컬럼을 하나로 합친다. USING 컬럼이 한쪽에 없으면 `unknown_field`, 왼쪽에 둘 이상이면 `ambiguous_field`다.
- 테이블 컬럼 별칭(`orders AS o(x, s)`)은 앞쪽 컬럼 이름을 바꾼다.
- SELECT 별칭은 같은 SELECT의 GROUP BY와 ORDER BY에서만 해석한다.
- 컬럼 목록을 알 수 없는 소스(목록 없는 테이블 함수)는 필드 검사를 생략한다. `unnest(...) AS t(x)`처럼 컬럼 목록을 쓰면 그 목록으로 검사한다.
- 리터럴 안 placeholder 검사는 앞이 글자나 `:`가 아닌 `:name`만 잡는다(`INTERVAL ':days days'`는 잡고 `'10:30'`, `'a::b'`는 잡지 않는다).
- `undeclared_relation`은 두 Context 테이블 사이의 `JOIN ... ON a = b`만 검사한다.
- `backend/tests/test_postgres_parity.py`는 개발 검사에서 실제 PostgreSQL로 SQL을 EXPLAIN해, 이름·구조 판정이 PostgreSQL과 같은지 확인한다. 제품 실행에는 쓰지 않는다. 이름 해석 규칙을 바꿀 때는 근거 SQL을 이 목록에 추가한다.

### 7.5 Tool 검증

Tool 검증(저장, 수정, 재검증, `input_schema`가 있는 `/validate`)은 `DEFAULT_POLICY`로 실행한다.
1. §4.1 `input_schema` 제약을 검사한다(`invalid_input_schema`).
2. `input_schema`의 입력을 선언된 파라미터로 삼아 SQL 검증을 실행한다. placeholder 집합과 입력 집합이 다르면 `undeclared_parameter` 또는 `unused_parameter`다.
3. 입력 enum과 Context 허용 값을 대조한다(`input_enum_conflict`).
4. 지표에 묶인 Tool은 계약과 대조한다(`metric_contract_mismatch`).
5. `checks`는 `input_schema`로 시작한다.

Tool에 저장된 값이 없으므로 `parameter_type_mismatch`는 Tool 검증에서 발생하지 않는다. invoke 시 실제 인수는 같은 정적 값 검증기로 검사한다.

---

## 8. 상태 전이

```
Question ──/generate──▶ Generation (메모리)
                          ├─ ambiguous     → Application이 사용자에게 clarifications를 묻는다
                          ├─ unanswerable  → Application이 실패를 처리한다
                          ├─ (failed)      → 422 generation_invalid
                          └─ ok            → parameterized SQL Operation 초안 (정적 검증 통과)
                                               │ [Save as Tool]
                                               ▼
                                         /tools/draft  (LLM 없음)
                                               │ 사용자 검토/편집
                                               ▼
                                         /tools  (검증 통과해야 저장 → Tool 파일)
                                               ▼
                                         Tool[valid]
                                               │
                     Context 변경 ─▶ 재검증 ──┬─▶ valid (context_version 갱신)
                                               └─▶ invalid ──(Tool 수정 후 재검증 통과)──▶ valid
                                               │
                                         /tools/{name}/invoke ──▶ SQL Operation
```

---

## 9. Evaluation

목적은 Generate 품질의 재현 가능한 regression suite다. SQL을 실행하지 않고, LLM에게 정답 여부를 판단하게 하지 않는다. 모든 suite는 개발과 리뷰 중에 노출되었으므로 **회귀 평가**이며, 일반 정확도의 근거로 쓰지 않는다.

### 9.1 Golden Case 형식

`evals/<suite>/context.json`과 `cases.yaml`로 구성한다. Context fixture는 생성 API와 같은 `name`, `dialect`, `timezone`, `definition`을 포함하며 평가 전용 이름(예: `eval-commerce`)을 쓴다. `cases.yaml`의 최상위에는 공통 `reference_time`과 `cases` 배열을 둔다. 케이스의 `reference_time`이 공통값보다 우선한다. 모든 기준 시각에는 timezone offset이 필요하다.

```yaml
reference_time: "2026-10-02T09:00:00+09:00"
cases:
  - id: busan_revenue
    category: parameterization
    question: 부산 지역 매출
    expect:
      status: ok
      resources: [orders, customers]
      fields: [orders.amount, customers.region]
      joins: [[orders.customer_id, customers.id]]
      aggregates: [{ function: sum, field: orders.amount }]
      constraints:
        - { field: orders.status, op: "=", value: COMPLETED }
        - { field: customers.region, op: "=", parameter_value: 부산 }
      parameters: [{ value: 부산, type: string }]
      forbidden_parameter_values: [COMPLETED]
      forbidden_literals: [부산]
      forbidden_constructs: [relative_time_function]
```

- `expect.status`는 필수다. 그 외 expectation은 생략할 수 있다. `source`(`metric` | `generated_sql`)도 지정할 수 있다.
- `fields` / `forbidden_fields`: 반드시 참조할 / 참조하면 안 되는 `resource.field`.
- `joins`: 두 필드의 equality join이 WHERE 또는 JOIN ON 조건에 있어야 한다. 좌우 순서는 무관하다.
- `aggregates`: SELECT projection에 필요한 집계(`count / sum / avg / min / max`, `field`는 `resource.field` 또는 `*`, `distinct` 기본 false). `COUNT(*)`, `COUNT(1)`, outer JOIN이 없는 쿼리의 non-null 필드 `COUNT(field)`는 row count로 취급한다.
- `group_by`: GROUP BY에 필요한 필드 목록.
- `constraints`: 필드, 비교 연산자, 필요하면 비교 대상(`value` | `parameter_value` | `parameter: true`, 셋 중 하나).
- `parameters`: 값과 선택적인 `type`으로 필요한 파라미터를 찾는다.
- `forbidden_parameter_values` / `forbidden_literals`: Rule 상수의 파라미터화, 사용자 값의 literal 고정 회귀를 잡는다.
- `forbidden_constructs`: `select_star`, `relative_time_function`.
- 선택적인 `reference`는 작성자가 만든 정답 Operation이다. 채점에는 쓰지 않고 테스트 fixture로 쓴다.
- 빈 suite, 중복 case id, 잘못된 timezone·Context 무결성, 모순된 비교 target은 실행 전에 실패한다.

### 9.2 채점

- 먼저 status를 비교한다. `ok`는 공개 응답의 validation 결과와 함께 정적 검증을 다시 실행한다.
- resources, fields, joins, aggregates, group_by, constraints, forbidden constructs는 sqlglot AST에서 추출한다. SQL 문자열을 비교하지 않는다. 채점기의 field 추적은 검증기와 독립된 구현이라, 검증기의 추적 오류가 채점에서 같이 가려지지 않는다.
- 비교 연산자는 `= / != / <> / IN / NOT IN / >= / <= / > / <`를 지원하고, NOT·AND·OR, 좌우 반전, 직접 필드의 cast/date wrapper, BETWEEN을 반영한다. 숫자는 Decimal로, 날짜는 Context timezone의 자정, date-time은 같은 순간인지로 비교한다.
- 이 evaluator는 명시된 AST 조건을 검사한다. 전체 의미 동등성, 집계 결과·행 수, 모든 경로에서의 Rule 적용을 증명하지 않는다.
- 케이스 결과는 pass/fail, 실패한 expectation과 설명, generation_id, reference_time, validation errors를 포함한다. 요청 오류도 실패로 기록하고 다음 케이스를 계속 실행한다.

commerce suite는 18개다(`ok` 12, `ambiguous` 3, `unanswerable` 3). 기간 케이스는 reference_time `2026-10-02T09:00:00+09:00`, timezone `Asia/Seoul`에서 반열린 범위로 검사한다.

### 9.3 CLI

실행 중인 Sidewit API에 요청한다. Provider와 model은 서버 설정을 따른다.

```bash
docker compose run --rm --no-deps -v ./evals:/evals:ro api sidewit-eval run /evals/commerce --api-url http://api:8000
```

- `--api-url` 기본값은 `http://localhost:8000`, `--timeout` 기본값은 120초다. 서버 인증이 켜져 있으면 `SIDEWIT_API_KEY` 또는 `--api-key`를 쓴다. `--case`로 케이스를 고르고, `--out`으로 JSON 리포트를 저장한다.
- suite 이름의 Context가 없으면 만들고, fixture와 다르면 `expected_version`으로 갱신한다. 평가 후 남는다.
- exit code: 전체 통과 0, 케이스 실패 1, 설정·연결·리포트 오류 2.
- 실제 provider 실행은 LLM을 호출하므로 CI 기본 실행에서 제외한다. CI의 FakeProvider 결과를 실제 모델 품질로 보고하지 않는다.

---

## 10. UI

브라우저에서 정확히 보존할 수 없는 JSON 숫자는 반올림해서 사용하지 않는다. API 응답은 `numeric_precision_loss` 오류로 표시하고, Metrics JSON은 원문을 유지하며 저장을 차단한다. 정수는 ±9007199254740991 범위이며 소수도 문자열 왕복 시 값이 같아야 한다. 범위 밖의 값은 정확한 숫자를 처리하는 API 클라이언트를 사용한다.

React SPA이며 화면은 3개다. API만으로도 모든 기능을 쓸 수 있으며 UI는 관리 편의 기능이다.

**Context**
- 목록과 생성
- DDL 붙여넣기 → import 미리보기(merge/replace) → 적용
- Resource/Field 편집(description, allowed_values는 enum 성격 field만)
- Relations, Business Rules(선택적 constant: field·연산자·값), Metrics JSON 편집
- **Allow model-written SQL**(`allow_generated_sql`)
- 저장할 때 버전 표시. 재검증 결과에 invalid Tool이 있으면 경고 배너

**Playground**
- Context 선택, 질문 입력, [Generate]. 지표가 있는 Context는 지표 선택과 **Answer only with a registered metric**(`require_metric`)을 제공한다.
- `ok`: Monaco SQL(읽기 전용), Parameters 표, 검증 결과와 경고, 출처 배지(metric contract / model-written SQL), 실행 안전성·접근 통제·비용·업무 정확성은 보장하지 않는다는 안내, [Copy SQL] [Copy JSON] [Save as Tool] 👍👎
- `ambiguous`: reason과 clarifications. `unanswerable`: reason.
- `422`: "Rejected generation"으로 표시한다. 거부된 SQL은 `GET /generations/{id}`로 가져와 접힌 상태로만 보여주고 정상 Operation과 구분한다(점선 빨간 테두리, "Not validated · Do not execute"). Copy 버튼이나 Tool draft 진입점을 두지 않는다.
- 최근 생성 기록은 서버 메모리의 기록이며 재시작하면 사라진다고 표시한다.

**Tools**
- 목록(name, context, status 배지, version, 지표에 묶인 Tool은 `metric` 배지)과 [New Tool]
- 편집 화면: name, description, 파라미터 편집기(name/type/format/description/enum), Monaco SQL 편집. 편집 중 `/validate`를 debounce로 호출해 에러를 인라인 표시하고, `NOW()` 허용과 Generate 호환성 검사가 아님을 함께 적는다.
- [Save]는 현재 편집 내용과 Context 버전에 대한 검증을 통과해야 활성화된다. Context 변경 후 재검증 대기·실패 중에는 이전 통과 결과로 저장을 허용하지 않는다. 다른 곳에서 Tool이 바뀌면 편집 내용을 보존한 채 알리고, 버전 충돌을 409로 받는다.
- 지표에 묶인 Tool은 SQL과 입력 이름·타입이 읽기 전용이고 **Rebuild from contract**로 갱신한다.
- Test invoke 패널: `example_arguments`를 기본값으로 쓰는 폼, 응답 JSON 표시

**구현 메모**
- SQL이 실행됐다는 인상을 주는 표현(`Run Query`, `Execute SQL` 등)은 쓰지 않는다. 결과는 "SQL operation" 또는 "Resolved SQL operation"이며 "not executed by Sidewit"을 함께 표시한다.
- Tool 입력 목록과 invoke 폼은 `input_schema.required` 배열 순서를 따른다.
- Monaco는 로컬 번들로 포함하고 SQL을 보여주는 화면에서만 지연 로드한다.
- API Key는 헤더의 대화상자에서 입력해 localStorage에 저장하고 `Authorization: Bearer`로 보낸다. 로컬·self-hosted 개발 도구를 위한 편의 기능이며 장기적인 인증 모델이 아니다.

---

## 11. 저장

Sidewit은 데이터베이스를 쓰지 않는다. 상태는 데이터 폴더(`SIDEWIT_DATA_DIR`, 기본 `/data`)의 파일이다. 배포 시 이 폴더를 볼륨으로 연결한다.

```
/data
├─ contexts/<context_id>.json
└─ tools/<tool_name>.json
```

- 파일은 UTF-8 JSON이며 키를 정렬하고 2칸 들여쓴다(사람이 diff로 읽을 수 있게). 숫자는 정밀도를 잃지 않고 그대로 저장한다.
- **시작 시** 모든 파일을 읽어 검증한다. 디렉터리 목록을 읽을 수 없거나 형식·무결성 오류(잘못된 JSON, Context 검증 실패, 이름 중복, 없는 Context를 가리키는 Tool 등)가 하나라도 있으면 경로와 원인을 출력하고 시작하지 않는다. 폴더가 없으면 빈 상태로 만들지만, 읽을 수 없는 폴더를 빈 상태로 취급하지 않는다.
- **쓰기**는 같은 폴더의 임시 파일에 쓴 뒤 fsync·rename·부모 폴더 fsync로 교체한다. 하나의 API 프로세스가 중간 await 없는 동기 처리로 변경을 직렬화한다. Context를 먼저 저장한 뒤 Tool 재검증 결과를 저장한다. 여러 파일 전체의 트랜잭션은 아니다. 도중 실패·중단되면 일부 변경이 남을 수 있으므로 클라이언트는 현재 버전을 다시 조회해야 한다. 이전 Context 버전의 Tool은 invoke 전에 재검증한다. 저장 실패 시 메모리와 검증 캐시를 복구하며, 파일을 다시 읽을 수 없으면 API는 503으로 닫고 재시작을 요구한다.
- 버전 충돌(`expected_version`)과 이름 충돌은 메모리의 현재 상태로 판정한다.
- Tool 검증 캐시는 저장소 인스턴스별로 보관한다. Context·Tool 저장 시 관련 판정을 무효화하고 복구·종료 시 모두 비운다. 캐시는 SQL 재파싱 전체를 생략하는 장치가 아니며, invoke의 실제 입력값 검사는 항상 수행한다.
- **인스턴스와 API worker는 하나만 쓴다.** 데이터 폴더의 `.lock`을 프로세스 수명 동안 잠그며 같은 폴더를 여는 두 번째 프로세스는 시작을 거부한다. 로컬 파일시스템을 사용한다. 네트워크 공유 볼륨과 여러 인스턴스는 지원하지 않는다.
- 실행 중에 파일을 직접 고치면 재시작해야 반영된다. 실행 중인 Sidewit이 그 파일을 덮어쓸 수 있으므로, 직접 편집은 Sidewit을 멈춘 상태에서 한다.
- 백업은 폴더 복사다. 폴더에는 Context(스키마 설명, 업무 규칙, 허용 값)가 들어 있으므로 접근 권한을 제한한다.
- Generation은 이 폴더에 저장하지 않는다(§3.4).
