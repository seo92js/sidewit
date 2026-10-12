import { expect, test } from "vitest";
import { placeholders } from "@/pages/tool/placeholders";

test.each([
  ["SELECT :id::bigint, :id, :other", ["id", "other"]],
  ["SELECT 1 -- :unused\n, :actual", ["actual"]],
  ["SELECT /* :a /* :b */ :c */ :actual", ["actual"]],
  ["SELECT $$:text$$, $tag$:other$tag$, :actual", ["actual"]],
  ["SELECT ':text', 'it''s :text', \"colon:name\", :actual", ["actual"]],
  ["SELECT E'it\\'s :text', :actual", ["actual"]],
  ["SELECT $tag$:unfinished", []],
  ["SELECT /* :unfinished", []],
  ["SELECT :_id, :UserId, :userId", []],
])("finds only real input hints in %s", (sql, expected) => {
  expect(placeholders(sql)).toEqual(expected);
});
