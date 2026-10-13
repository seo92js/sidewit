import { useState } from "react";
import type { ClarificationAnswer, GenerateResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

type Ambiguous = Extract<GenerateResponse, { status: "ambiguous" }>;

export function ClarificationForm({
  result,
  originalQuestion = "",
  previousAnswers = [],
  blockedReason,
  pending,
  onContinue,
}: {
  result: Ambiguous;
  originalQuestion?: string;
  previousAnswers?: ClarificationAnswer[];
  blockedReason: string | null;
  pending: boolean;
  onContinue: (answers: ClarificationAnswer[]) => void;
}) {
  const disabled = !!blockedReason;
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(previousAnswers.map((answer) => [answer.id, answer.answer])),
  );
  const questions = [
    ...previousAnswers.map((answer) => ({ ...answer, options: [] as string[] })),
    ...result.clarification_questions,
  ];
  const characters =
    Array.from(originalQuestion).length +
    questions.reduce(
      (total, q) =>
        total + Array.from(q.question).length + Array.from(values[q.id]?.trim() ?? "").length,
      0,
    );
  const limitError =
    characters > 16000
      ? "Question and clarification answers exceed 16,000 characters. Shorten your answers or start a new request."
      : questions.length > 24
        ? "A request supports at most 24 clarification answers. Start a new request."
        : null;
  const complete = questions.length > 0 && questions.every((q) => values[q.id]?.trim());
  return (
    <Card className="border-amber-300">
      <CardHeader>
        <CardTitle className="text-base">The question needs clarification</CardTitle>
        <p className="font-mono text-xs text-muted-foreground">
          ambiguous · generation {result.generation_id.slice(0, 8)} · context v
          {result.context_version}
        </p>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p>{result.reason}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (disabled || pending || !complete || limitError) return;
            onContinue(
              questions.map((q) => ({
                id: q.id,
                question: q.question,
                answer: values[q.id].trim(),
              })),
            );
          }}
          className="space-y-4"
        >
          <fieldset disabled={disabled || pending} className="space-y-4">
            {questions.map((q) => (
              <div key={q.id} className="space-y-2">
                <Label htmlFor={`answer-${q.id}`}>{q.question}</Label>
                {q.options.length > 0 && (
                  <div className="flex flex-wrap gap-2" role="group" aria-label={q.question}>
                    {q.options.map((option) => (
                      <Button
                        key={option}
                        type="button"
                        size="sm"
                        className="h-auto max-w-full whitespace-normal break-words py-1 text-left"
                        variant={values[q.id] === option ? "default" : "outline"}
                        aria-pressed={values[q.id] === option}
                        onClick={() => setValues((previous) => ({ ...previous, [q.id]: option }))}
                      >
                        {option}
                      </Button>
                    ))}
                  </div>
                )}
                <Textarea
                  id={`answer-${q.id}`}
                  rows={2}
                  maxLength={2000}
                  placeholder="Choose an option or enter your answer"
                  value={values[q.id] ?? ""}
                  onChange={(e) =>
                    setValues((previous) => ({ ...previous, [q.id]: e.target.value }))
                  }
                />
              </div>
            ))}
            {limitError && (
              <p role="alert" className="text-destructive">
                {limitError}
              </p>
            )}
            <Button type="submit" disabled={!complete || !!limitError}>
              {pending ? "Continuing…" : "Continue with answers"}
            </Button>
          </fieldset>
        </form>
        <p className="text-xs text-muted-foreground">
          {blockedReason ??
            "Your answers refine the original request. Sidewit checks saved Tools and validates the operation again. Nothing is executed."}
        </p>
      </CardContent>
    </Card>
  );
}
