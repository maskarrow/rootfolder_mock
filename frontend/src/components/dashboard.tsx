"use client";

import { useRef, useState } from "react";

import {
  checkProvider,
  downloadHref,
  getMe,
  listFiles,
  listItems,
  runStream,
  searchItems,
  sendTestEmail,
  similarItems,
  type StoredFile,
  uploadFile,
} from "@/lib/api";

/** Every check in the browser, through `/api`, the way a user's requests travel in
 *  production (browser → nginx → backend). Each card shows the raw JSON answer or
 *  the error, so a deploy problem shows as it is. */

type Outcome = { pending: boolean; output: string | null; failed: boolean };

const IDLE: Outcome = { pending: false, output: null, failed: false };

function useCheck() {
  const [outcome, setOutcome] = useState<Outcome>(IDLE);

  async function run(task: () => Promise<unknown>) {
    setOutcome({ pending: true, output: null, failed: false });
    try {
      const value = await task();
      setOutcome({ pending: false, output: JSON.stringify(value, null, 2), failed: false });
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      setOutcome({ pending: false, output: text, failed: true });
    }
  }

  return [outcome, run] as const;
}

function Output({ outcome }: { outcome: Outcome }) {
  if (outcome.pending) return <p className="muted">Working…</p>;
  if (outcome.output === null) return null;
  return <pre className={outcome.failed ? "output failed" : "output"}>{outcome.output}</pre>;
}

function Card({
  title,
  proves,
  children,
}: {
  title: string;
  proves: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <h2>{title}</h2>
      <p className="muted">{proves}</p>
      {children}
    </section>
  );
}

function SimpleCheck({
  title,
  proves,
  label,
  task,
}: {
  title: string;
  proves: string;
  label: string;
  task: () => Promise<unknown>;
}) {
  const [outcome, run] = useCheck();
  return (
    <Card title={title} proves={proves}>
      <button type="button" onClick={() => void run(task)} disabled={outcome.pending}>
        {label}
      </button>
      <Output outcome={outcome} />
    </Card>
  );
}

function SearchCheck() {
  const [query, setQuery] = useState("licitație garanții");
  const [outcome, run] = useCheck();
  return (
    <Card
      title="Search"
      proves="Full-text search with the romanian configuration: stems match, so the singular finds the plural."
    >
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          void run(() => searchItems(query));
        }}
      >
        <input value={query} onChange={(event) => setQuery(event.target.value)} required />
        <button type="submit" disabled={outcome.pending}>
          Search
        </button>
      </form>
      <Output outcome={outcome} />
    </Card>
  );
}

async function similarToFirst() {
  const items = await listItems();
  if (items.length === 0) throw new Error("No items yet: run `just seed`.");
  return similarItems(items[0].id);
}

function formatSize(bytes: number): string {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${(bytes / 1024).toFixed(1)} KB`;
}

function Files({ initialFiles }: { initialFiles: StoredFile[] }) {
  const [files, setFiles] = useState(initialFiles);
  const [progress, setProgress] = useState<number | null>(null);
  const [outcome, run] = useCheck();
  const [listError, setListError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setFiles(await listFiles());
      setListError(null);
    } catch (error) {
      setListError(error instanceof Error ? error.message : String(error));
    }
  }

  async function upload() {
    const file = picker.current?.files?.[0];
    if (!file) return;
    setProgress(0);
    await run(async () => {
      try {
        return await uploadFile(file, setProgress);
      } finally {
        setProgress(null);
        await refresh();
      }
    });
  }

  return (
    <Card
      title="Upload PDF"
      proves="The proxy's body size limit and streaming, the storage volume, relative paths. After upload the file is processing for JOB_SECONDS; a deploy in that window leaves it interrupted."
    >
      <div className="row">
        <input ref={picker} type="file" accept=".pdf,application/pdf" />
        <button type="button" onClick={() => void upload()} disabled={outcome.pending}>
          Upload
        </button>
      </div>
      {progress !== null && (
        <div className="progress" aria-label="Upload progress">
          <div style={{ width: `${Math.round(progress * 100)}%` }} />
          <span>{Math.round(progress * 100)}%</span>
        </div>
      )}
      <Output outcome={outcome} />

      <div className="row spaced">
        <h3>Files</h3>
        <button type="button" className="secondary" onClick={() => void refresh()}>
          Refresh
        </button>
      </div>
      {listError && <p className="error">{listError}</p>}
      {files.length === 0 ? (
        <p className="muted">No files yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Size</th>
              <th>Status</th>
              <th>Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.id}>
                <td>
                  {/* A plain link: the browser sends the cookie and saves the file. */}
                  <a href={downloadHref(file.id)}>{file.filename}</a>
                </td>
                <td>{formatSize(file.size_bytes)}</td>
                <td className={`status ${file.status}`}>{file.status}</td>
                <td>{new Date(file.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

type Mark = { label: string; at: number };

function StreamCheck() {
  const [running, setRunning] = useState(false);
  const [text, setText] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [marks, setMarks] = useState<Mark[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    const started = performance.now();
    const since = () => (performance.now() - started) / 1000;
    const timer = window.setInterval(() => setElapsed(since()), 100);
    const mark = (label: string) => setMarks((all) => [...all, { label, at: since() }]);
    let firstDelta = true;

    setRunning(true);
    setText("");
    setMarks([]);
    setError(null);
    try {
      await runStream(({ event, data }) => {
        if (event === "delta") {
          if (firstDelta) mark("first delta");
          firstDelta = false;
          setText((all) => all + (data as { text: string }).text);
        } else {
          mark(event);
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      window.clearInterval(timer);
      setElapsed(since());
      setRunning(false);
    }
  }

  return (
    <Card
      title="Stream test"
      proves="No buffering and a long read timeout on the way: a status event, a silent phase (with a keep-alive ping every 15 s), then 40 deltas 150 ms apart. If they all land at the same moment as done, something buffers."
    >
      <div className="row">
        <button type="button" onClick={() => void start()} disabled={running}>
          Start stream
        </button>
        <span className="muted">{elapsed.toFixed(1)} s</span>
      </div>
      {marks.length > 0 && (
        <ul className="marks">
          {marks.map((m, i) => (
            <li key={i}>
              {m.label} at {m.at.toFixed(1)} s
            </li>
          ))}
        </ul>
      )}
      {text && <pre className="output">{text}</pre>}
      {error && <pre className="output failed">{error}</pre>}
    </Card>
  );
}

export function Dashboard({
  isAdmin,
  initialFiles,
}: {
  isAdmin: boolean;
  initialFiles: StoredFile[];
}) {
  return (
    <>
      <SimpleCheck
        title="Who am I via /api"
        proves="Asked by the browser through the proxy: seen_ip must be your own address and scheme https, or the proxy's forwarded headers do not reach uvicorn."
        label="Who am I"
        task={getMe}
      />
      <SimpleCheck
        title="Retrieve items"
        proves="The migrations ran: 20 newest items of your org, the pinned one first (the column from the second migration)."
        label="Retrieve items"
        task={listItems}
      />
      <SearchCheck />
      <SimpleCheck
        title="Similar to first item"
        proves="pgvector: the 5 nearest items by cosine distance, from the HNSW index."
        label="Find similar"
        task={similarToFirst}
      />
      <Files initialFiles={initialFiles} />
      <StreamCheck />
      {isAdmin && (
        <>
          <SimpleCheck
            title="Check provider key"
            proves="Outbound HTTPS from the server, the master key, and the org's encrypted key: lists Anthropic's models, which bills nothing."
            label="Check provider key"
            task={checkProvider}
          />
          <SimpleCheck
            title="Send test email"
            proves="The Resend key and sender. The test sender only delivers to the Resend account's own address."
            label="Send test email"
            task={sendTestEmail}
          />
        </>
      )}
    </>
  );
}
