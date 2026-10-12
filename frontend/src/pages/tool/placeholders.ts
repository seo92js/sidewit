/** Editor hints only; the server remains responsible for SQL validation. */
export function placeholders(sql: string): string[] {
  const names = new Set<string>();
  let i = 0;
  while (i < sql.length) {
    if (sql.startsWith("--", i)) {
      const end = sql.indexOf("\n", i + 2);
      i = end < 0 ? sql.length : end + 1;
    } else if (sql.startsWith("/*", i)) {
      let depth = 1;
      i += 2;
      while (i < sql.length && depth) {
        if (sql.startsWith("/*", i)) {
          depth++;
          i += 2;
        } else if (sql.startsWith("*/", i)) {
          depth--;
          i += 2;
        } else i++;
      }
    } else if (sql[i] === "'" || sql[i] === '"' || sql[i] === "`") {
      const quote = sql[i++];
      const escaped = quote === "'" && /(?:^|[^\w$])[eE]$/.test(sql.slice(0, i - 1));
      while (i < sql.length) {
        if (escaped && sql[i] === "\\") {
          i += 2;
        } else if (sql[i++] === quote) {
          if (sql[i] === quote) i++;
          else break;
        }
      }
    } else if (sql[i] === "$" && !/[\w$]/.test(sql[i - 1] ?? "")) {
      const delimiter = sql.slice(i).match(/^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$/)?.[0];
      if (delimiter) {
        const end = sql.indexOf(delimiter, i + delimiter.length);
        i = end < 0 ? sql.length : end + delimiter.length;
      } else i++;
    } else if (sql[i] === ":" && !/[:\w$]/.test(sql[i - 1] ?? "")) {
      const name = sql.slice(i + 1).match(/^[a-z][a-z0-9_]*(?![\w$])/)?.[0];
      if (name && name.length <= 64) names.add(name);
      i++;
    } else i++;
  }
  return [...names];
}
