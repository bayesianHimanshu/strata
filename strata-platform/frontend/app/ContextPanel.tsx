"use client";

import { useEffect, useState } from "react";
import {
  api,
  prepareContext,
  type ContextProgress,
  type ContextStatus,
  type Decision,
} from "../lib/api";

const ALL_CONNECTORS = ["clinicaltrials", "pubmed", "openfda", "nice"];
const DEFAULT_CONNECTORS = ["clinicaltrials", "pubmed", "openfda"];
const LABEL: Record<string, string> = {
  clinicaltrials: "ClinicalTrials.gov",
  pubmed: "PubMed",
  openfda: "openFDA",
  nice: "NICE (horizon)",
};

export type ContextMode = { boundary_mode: "backtest" | "live"; as_of?: string };

export default function ContextPanel({
  decision,
  onMode,
}: {
  decision: Decision | null;
  onMode: (m: ContextMode) => void;
}) {
  const [mode, setMode] = useState<"backtest" | "live">("backtest");
  const [asOf, setAsOf] = useState<string>(new Date().toISOString().slice(0, 10));
  const [conns, setConns] = useState<string[]>(DEFAULT_CONNECTORS);
  const [progress, setProgress] = useState<ContextProgress[]>([]);
  const [status, setStatus] = useState<ContextStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  // external-source inputs
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");

  useEffect(() => {
    onMode({ boundary_mode: mode, as_of: mode === "live" ? asOf : undefined });
  }, [mode, asOf]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (decision?.drug) api.contextStatus(decision.drug).then(setStatus).catch(() => {});
  }, [decision?.drug]);

  function refreshStatus() {
    if (decision?.drug) api.contextStatus(decision.drug).then(setStatus).catch(() => {});
  }

  async function prepare() {
    if (!decision) return;
    setBusy(true); setMsg(null); setProgress([]);
    try {
      await prepareContext(
        { drug: decision.drug, indication: decision.indication, as_of: asOf,
          mode, connectors: conns },
        setProgress,
      );
      refreshStatus();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function addSource(kind: "url" | "text" | "file", value: string, filename?: string) {
    if (!decision || !value.trim()) return;
    setMsg(null);
    try {
      const r = await api.addContext({
        kind, value, drug: decision.drug, indication: decision.indication,
        doc_date: mode === "live" ? asOf : decision.decision_date, filename,
      });
      setMsg(`Added ${r.doc_type} source (${r.chunks} chunks) - re-run to see it cited.`);
      setUrl(""); setText(""); refreshStatus();
    } catch (e) {
      setMsg(String(e));
    }
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => addSource("file", String(reader.result || ""), f.name);
    reader.readAsText(f);
  }

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="eyebrow" style={{ marginBottom: 10 }}>Real-time context</div>

      {/* mode toggle */}
      <div className="controls" style={{ alignItems: "center", gap: 14 }}>
        <div className="chip-select">
          <button className={`dchip ${mode === "backtest" ? "sel" : ""}`}
            onClick={() => setMode("backtest")}>
            <div className="dr">Backtest</div><div className="ind">as-of decision date</div>
          </button>
          <button className={`dchip ${mode === "live" ? "sel" : ""}`}
            onClick={() => setMode("live")}>
            <div className="dr">Live</div><div className="ind">as-of today / chosen date</div>
          </button>
        </div>
        {mode === "live" && (
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
            className="dchip" style={{ padding: "8px 10px" }} />
        )}
      </div>

      {/* connector toggles + freshness */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
        {ALL_CONNECTORS.map((k) => {
          const on = conns.includes(k);
          const fresh = status?.freshness?.[k];
          return (
            <button key={k} className={`dchip ${on ? "sel" : ""}`}
              onClick={() => setConns(on ? conns.filter((c) => c !== k) : [...conns, k])}>
              <div className="dr">{LABEL[k]}</div>
              <div className="ind">{fresh ? `fresh ${fresh.slice(0, 10)}` : "not fetched"}</div>
            </button>
          );
        })}
      </div>

      <div style={{ marginTop: 12, display: "flex", gap: 10, alignItems: "center" }}>
        <button className="btn" onClick={prepare} disabled={busy || !decision}>
          {busy ? "Preparing context…" : "Prepare context"}
        </button>
        {progress.map((p) => (
          <span className="mono" key={p.connector} style={{ fontSize: 12 }}>
            {LABEL[p.connector] ?? p.connector}… {p.state}{p.count ? ` ${p.count}` : ""}
          </span>
        ))}
      </div>

      {/* add external source */}
      <div className="section-label" style={{ margin: "16px 0 10px" }}>
        <span className="eyebrow">Add an external source</span><span className="rule" />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", gap: 8 }}>
          <input placeholder="https://… (allowlisted domains only)" value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="dchip" style={{ flex: 1, padding: "9px 12px" }} />
          <button className="btn" onClick={() => addSource("url", url)} disabled={!url}>Add URL</button>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <textarea placeholder="Paste evidence text…" value={text}
            onChange={(e) => setText(e.target.value)}
            className="dchip" style={{ flex: 1, padding: "9px 12px", minHeight: 56 }} />
          <button className="btn" onClick={() => addSource("text", text)} disabled={!text}>Add text</button>
        </div>
        <label className="mono" style={{ fontSize: 12, color: "var(--ink-soft)" }}>
          Upload (.txt/.pdf/.docx): <input type="file" accept=".txt,.pdf,.docx" onChange={onFile} />
        </label>
      </div>

      {status && (
        <div className="note">
          indexed for {decision?.drug}: {status.indexed_chunks} chunks
          {Object.entries(status.by_source).map(([k, n]) => ` · ${k} ${n}`)}
        </div>
      )}
      {msg && <div className="note" style={{ color: "var(--grounded)" }}>{msg}</div>}
    </div>
  );
}
