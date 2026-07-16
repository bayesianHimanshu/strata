"use client";

import { useEffect, useState } from "react";
import ContextPanel, { type ContextMode } from "../ContextPanel";
import {
  api,
  runToCompletion,
  type Decision,
  type Job,
  type Vulnerability,
} from "../../lib/api";

const PRETTY: Record<string, string> = {
  comparator: "Inappropriate comparator",
  icer_uncertainty: "Cost-effectiveness (ICER) uncertainty",
  missing_pro: "Missing patient-reported outcomes",
  surrogate_endpoint_immaturity: "Immature surrogate endpoint",
  trial_design_bias: "Trial-design bias",
  generalizability: "Generalisability to practice",
  budget_impact: "Budget impact",
  other: "Other concern",
};

function VChip({ v }: { v: Vulnerability }) {
  const [open, setOpen] = useState(false);
  const srcs = v.provenance?.source_ids ?? [];
  return (
    <div className={`vchip ${v.grounded ? "grounded" : "parametric"}`}>
      <span className="cat">{PRETTY[v.category] ?? v.category}</span>
      {v.grounded && srcs.length > 0 && (
        <>
          <div className="prov-toggle" onClick={() => setOpen(!open)}>
            {open ? "▾ hide sources" : `▸ ${srcs.length} source${srcs.length > 1 ? "s" : ""}`}
          </div>
          {open && (
            <div className="prov">
              {srcs.map((s, i) => (
                <div className="src" key={i}>{s}</div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Column({ job, mode }: { job: Job | null; mode: "closed" | "open" }) {
  const vulns = job?.result?.vulnerabilities ?? [];
  return (
    <div className={`col ${mode}`}>
      <header>
        <div className="mode">{mode === "closed" ? "Closed book" : "Open book"}</div>
        <div className="sub">
          {mode === "closed"
            ? "parametric - no retrieval, the model's prior alone"
            : "grounded - retrieval under the leakage boundary"}
        </div>
      </header>
      <div className="body">
        {!job && <span className="empty">-</span>}
        {job && vulns.length === 0 && (
          <span className="empty">No concerns asserted.</span>
        )}
        {vulns.map((v, i) => (
          <VChip key={i} v={v} />
        ))}
      </div>
    </div>
  );
}

export default function HTA() {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [sel, setSel] = useState<Decision | null>(null);
  const [closed, setClosed] = useState<Job | null>(null);
  const [open, setOpen] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ctx, setCtx] = useState<ContextMode>({ boundary_mode: "backtest" });

  useEffect(() => {
    api.samples()
      .then((r) => {
        setDecisions(r.decisions);
        setSel(r.decisions[0] ?? null);
      })
      .catch(() => setErr("Can't reach the platform API."));
  }, []);

  async function run() {
    if (!sel) return;
    setRunning(true);
    setErr(null);
    setClosed(null);
    setOpen(null);
    try {
      await api.seed(); // ensure the open-book corpus is loaded (demo)
      const base = { boundary_mode: ctx.boundary_mode, as_of: ctx.as_of };
      const [c, o] = await Promise.all([
        runToCompletion("hta_archaeology", sel, { ...base, mode: "closed_book" }),
        runToCompletion("hta_archaeology", sel, { ...base, mode: "open_book" }),
      ]);
      setClosed(c);
      setOpen(o);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  }

  const boundary = (open?.result?.payload?.boundary ?? null) as
    | { cutoff?: string; buffer_days?: number; molecules?: string[]; exclude_siblings?: boolean }
    | null;
  const retrieved = open?.result?.payload?.retrieved_chunks as number | undefined;

  return (
    <>
      <section className="hero" style={{ paddingBottom: 20 }}>
        <div className="eyebrow">Capability · validated</div>
        <h1 style={{ fontSize: 30 }}>HTA Archaeology</h1>
        <p>
          Pick an appraisal and run it both ways. Watch the model go from reciting the
          genre of concerns to asserting only what the retrieved evidence supports -
          each grounded claim traceable to its sources.
        </p>
      </section>

      <ContextPanel decision={sel} onMode={setCtx} />

      <div className="panel">
        <div className="eyebrow" style={{ marginBottom: 10 }}>Choose a decision</div>
        <div className="chip-select">
          {decisions.map((d) => (
            <button
              key={d.decision_id}
              className={`dchip ${sel?.decision_id === d.decision_id ? "sel" : ""}`}
              onClick={() => setSel(d)}
            >
              <div className="id">{d.decision_id} · {d.decision_date}</div>
              <div className="dr">{d.drug}</div>
              <div className="ind">{d.indication}</div>
            </button>
          ))}
        </div>
        <div style={{ marginTop: 16 }}>
          <button className="btn" onClick={run} disabled={running || !sel}>
            {running ? "Running analysis…" : "Run analysis"}
          </button>
          {running && <span className="spinner" style={{ marginLeft: 12 }}>contacting model…</span>}
        </div>
        {err && <div className="note" style={{ color: "var(--excluded)" }}>{err}</div>}
      </div>

      {(closed || open || running) && (
        <div className="compare">
          <Column job={closed} mode="closed" />
          <Column job={open} mode="open" />
        </div>
      )}

      {boundary && (
        <div className="boundary">
          <h4>Leakage boundary enforced</h4>
          <div className="kv"><span className="k">retrieval cutoff</span>
            <span className="mono">{boundary.cutoff} (decision − {boundary.buffer_days}d)</span></div>
          <div className="kv"><span className="k">molecule scope</span>
            <span className="mono">{(boundary.molecules ?? []).join(", ")}</span></div>
          <div className="kv"><span className="k">own dossier</span>
            <span className="mono excluded-tag">excluded from retrieval</span></div>
          <div className="kv"><span className="k">same-drug siblings</span>
            <span className="mono excluded-tag">
              {boundary.exclude_siblings ? "excluded (registered policy)" : "included"}
            </span></div>
          <div className="kv"><span className="k">evidence retrieved</span>
            <span className="mono">{retrieved ?? 0} chunks within boundary</span></div>
          <div className="note">
            Closed-book never touches the store; open-book sees only evidence that passed
            this boundary. That is what makes the comparison honest.
          </div>
        </div>
      )}
    </>
  );
}
