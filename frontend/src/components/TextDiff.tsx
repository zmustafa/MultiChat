import { useMemo } from "react";

type Op = { kind: "same" | "add" | "del"; text: string };

function tokenize(text: string): string[] {
  return (text || "").split(/(\s+)/).filter((t) => t.length > 0);
}

/**
 * Word-level diff via a longest-common-subsequence table.
 *
 * Answers are capped before diffing: the table is O(n·m), and a revision that long is
 * better read side by side anyway.
 */
function diffWords(a: string[], b: string[]): Op[] {
  const n = a.length;
  const m = b.length;
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i][j] =
        a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  const push = (kind: Op["kind"], text: string) => {
    const last = ops[ops.length - 1];
    if (last && last.kind === kind) last.text += text;
    else ops.push({ kind, text });
  };
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push("same", a[i]);
      i++;
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      push("del", a[i]);
      i++;
    } else {
      push("add", b[j]);
      j++;
    }
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("add", b[j++]);
  return ops;
}

const MAX_WORDS = 3000;

/** Show what a model actually changed between two rounds. */
export function TextDiff({
  before,
  after,
  labelBefore = "before",
  labelAfter = "after",
}: {
  before: string;
  after: string;
  labelBefore?: string;
  labelAfter?: string;
}) {
  const ops = useMemo(
    () => diffWords(tokenize(before).slice(0, MAX_WORDS), tokenize(after).slice(0, MAX_WORDS)),
    [before, after],
  );
  const changed = ops.some((o) => o.kind !== "same");

  return (
    <div className="text-xs">
      <div className="mb-1 flex gap-3 text-[10px] text-gray-400">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-rose-400" />
          removed in {labelBefore}
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-emerald-400" />
          added in {labelAfter}
        </span>
      </div>
      {!changed ? (
        <p className="italic text-gray-400">No wording changed between these rounds.</p>
      ) : (
        <p className="whitespace-pre-wrap leading-relaxed text-gray-700 dark:text-gray-200">
          {ops.map((op, index) =>
            op.kind === "same" ? (
              <span key={index}>{op.text}</span>
            ) : op.kind === "add" ? (
              <ins
                key={index}
                className="bg-emerald-100 no-underline dark:bg-emerald-950/70 dark:text-emerald-200"
              >
                {op.text}
              </ins>
            ) : (
              <del key={index} className="bg-rose-100 dark:bg-rose-950/70 dark:text-rose-200">
                {op.text}
              </del>
            ),
          )}
        </p>
      )}
    </div>
  );
}
