import { createContext, isValidElement, memo, useContext, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Mermaid } from "./Mermaid";
import { MarkdownTable } from "./MarkdownTable";
import { delimitedToMarkdown } from "../utils/contentMeta";
import { mediaUrl } from "../api/client";

/**
 * Lets a lane-level "collapse/expand all code" control drive every CodeBlock beneath it.
 * `signal` bumps on each bulk command; CodeBlocks apply `collapsed` when it changes, while
 * still allowing an individual block to be toggled on its own afterwards.
 */
export const CodeFoldContext = createContext<{ signal: number; collapsed: boolean }>({
  signal: 0,
  collapsed: false,
});

/** Flatten syntax-highlighted React nodes into the original code text. */
function nodeText(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) {
    return nodeText((node.props as { children?: React.ReactNode }).children);
  }
  return "";
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className="rounded bg-gray-700 px-2 py-0.5 text-xs text-gray-100 opacity-70 hover:opacity-100"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** A fenced code block that can optionally re-render delimited data as a table. */
function CodeBlock({
  className,
  text,
  children,
  rest,
}: {
  className?: string;
  text: string;
  children: React.ReactNode;
  rest: Record<string, unknown>;
}) {
  const [asTable, setAsTable] = useState(false);
  const tableMd = useMemo(() => delimitedToMarkdown(text), [text]);
  const fold = useContext(CodeFoldContext);
  const [collapsed, setCollapsed] = useState(false);
  const lineCount = useMemo(() => text.split("\n").length, [text]);
  const lang = /language-(\w+)/.exec(className || "")?.[1];
  // Apply a lane-level "collapse/expand all" command whenever it fires.
  useEffect(() => {
    if (fold.signal > 0) setCollapsed(fold.collapsed);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fold.signal]);

  if (asTable && tableMd) {
    return (
      <div className="my-1">
        <button
          onClick={() => setAsTable(false)}
          className="mb-1 rounded border border-gray-300 px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
        >
          ‹ Show code
        </button>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{ table: ({ children }) => <MarkdownTable>{children}</MarkdownTable> }}
        >
          {tableMd}
        </ReactMarkdown>
      </div>
    );
  }

  return (
    <span className="relative block">
      <span className="absolute right-2 top-2 z-10 flex gap-1">
        {tableMd && (
          <button
            onClick={() => setAsTable(true)}
            title="Render this data as an interactive table"
            className="rounded bg-gray-700 px-2 py-0.5 text-xs text-gray-100 opacity-70 hover:opacity-100"
          >
            ⊞ Table
          </button>
        )}
        <button
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand code" : "Collapse code"}
          className="rounded bg-gray-700 px-2 py-0.5 text-xs text-gray-100 opacity-70 hover:opacity-100"
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <CopyButton text={text} />
      </span>
      {collapsed ? (
        <button
          onClick={() => setCollapsed(false)}
          className="block w-full py-1 pr-24 text-left text-xs italic text-gray-400 hover:text-gray-200"
        >
          ▸ {lineCount} line{lineCount === 1 ? "" : "s"} of code
          {lang ? ` · ${lang}` : ""} — click to expand
        </button>
      ) : (
        <code className={className} {...rest}>
          {children}
        </code>
      )}
    </span>
  );
}

// Module-level, stable references so ReactMarkdown never re-parses just because a parent
// re-rendered (e.g. another lane streaming). Combined with the useMemo below, a message's
// markdown is parsed only when its own content string changes.
const REMARK_PLUGINS = [remarkGfm];
const REHYPE_PLUGINS = [rehypeHighlight];
const MARKDOWN_COMPONENTS = {
  table({ children }: { children?: React.ReactNode }) {
    return <MarkdownTable>{children}</MarkdownTable>;
  },
  a({ href, children, ...props }: any) {
    const raw = href || "";
    const resolved = mediaUrl(raw);
    // Backend-served files (e.g. generated .pptx decks) download; other links open in a tab.
    const isApiFile = raw.startsWith("/api/files/");
    return (
      <a
        href={resolved}
        target="_blank"
        rel="noopener noreferrer"
        {...(isApiFile ? { download: true } : {})}
        {...props}
      >
        {children}
      </a>
    );
  },
  code({ className, children, ...props }: any) {
    const raw = nodeText(children);
    const text = raw.replace(/\n$/, "");
    const lang = /language-(\w+)/.exec(className || "")?.[1];
    // A fenced block (even a single line) arrives with a trailing newline in `raw`, which
    // distinguishes it from true inline code — check `raw`, not the trimmed `text`.
    const isBlock = className?.includes("language-") || raw.includes("\n");
    if (lang === "mermaid") {
      return <Mermaid code={text} />;
    }
    if (!isBlock) {
      return (
        <code className="rounded bg-gray-200 px-1 py-0.5 text-gray-800 dark:bg-gray-700 dark:text-gray-100" {...props}>
          {children}
        </code>
      );
    }
    return (
      <CodeBlock className={className} text={text} rest={props}>
        {children}
      </CodeBlock>
    );
  },
};

export const MessageRenderer = memo(function MessageRenderer({
  content,
}: {
  content: string;
}) {
  const body = useMemo(
    () => (
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    ),
    [content],
  );
  return (
    <div className="markdown text-sm text-gray-800 dark:text-gray-100">{body}</div>
  );
});

/** Below this length the whole answer is cheap to re-parse; splitting isn't worth it. */
const STREAM_SPLIT_MIN_CHARS = 1500;

/**
 * Split streamed markdown into a completed prefix and the block still being written.
 *
 * Re-parsing the whole accumulated answer on every flush is quadratic in the answer's
 * length, which is what made long responses jank. Cutting at the last block boundary lets
 * the prefix hit the memo and leaves only the final block to re-parse.
 *
 * The cut must never land inside an open code fence, or the half-written block would
 * render as prose and then snap back to code — so an unterminated fence pushes the cut
 * before its opening line.
 */
export function splitStreamingMarkdown(content: string): { stable: string; tail: string } {
  if (content.length < STREAM_SPLIT_MIN_CHARS) return { stable: "", tail: content };

  const fences: number[] = [];
  const fenceRe = /^```/gm;
  let match: RegExpExecArray | null;
  while ((match = fenceRe.exec(content)) !== null) fences.push(match.index);

  // An odd count means the last fence opened a block that hasn't closed yet.
  const limit = fences.length % 2 === 1 ? fences[fences.length - 1] : content.length;
  const cut = content.lastIndexOf("\n\n", limit - 1);
  if (cut <= 0) return { stable: "", tail: content };
  return { stable: content.slice(0, cut), tail: content.slice(cut) };
}

/**
 * Markdown for an answer that is still streaming. Renders the settled blocks and the
 * in-progress block as two memoized subtrees so each flush only re-parses the latter.
 */
export const StreamingMessage = memo(function StreamingMessage({
  content,
}: {
  content: string;
}) {
  const { stable, tail } = useMemo(() => splitStreamingMarkdown(content), [content]);
  if (!stable) return <MessageRenderer content={tail} />;
  return (
    <>
      <MessageRenderer content={stable} />
      <MessageRenderer content={tail} />
    </>
  );
});
