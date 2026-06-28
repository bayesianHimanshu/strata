"use client";

import { useState } from "react";
import { runParamsToCompletion, type SignalOutput, type SignalRow } from "../../lib/api";

const SAMPLES = [
  "Strongest disproportionality signals for osimertinib?",
  "Which adverse events are flagged for pembrolizumab?",
  "Cardiac signals for nivolumab?",
];

function Row({ r }: { r: SignalRow }) {
  const cells = `2×2: a=${r.a} (drug+event) · b=${r.b} · c=${r.c} · d=${r.d} · N=${r.n_total}`;
  return (
    <div className={`vchip ${r.signal_flag ? "parametric" : "grounded"}`} title={cells}>
      <span className="cat">
        {r.signal_flag && (
          <span className="badge" style={{ background: "var(--excluded-bg)", color: "var(--excluded)", marginRight: 6 }}>
            signal
          </span>
        )}
        {r.event_pt}
        <span className="mono" style={{ marginLeft: 10, color: "var(--ink-soft)" }}>
          a={r.a} · PRR {r.prr ?? "—"} · ROR {r.ror ?? "—"}
        </span>
      </span>
    </div>
  );
}

export default function Surveillance() {
  const [question, setQuestion] = useState(SAMPLES[0]);
  const [drug, setDrug] = useState("osimertinib");
  const [minCount, setMinCount] = useState(3);
  const [res, setRes] = useState<SignalOutput | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    if (!question.trim()) return;
    setRunning(true); setErr(null); setRes(null);
    try {
      const params: Record<string, unknown> = { question, min_count: minCount };
      if (drug.trim()) params.drug = drug.trim();
      const job = await runParamsToCompletion("safety_surveillance", params, 120000);
      if (job.status === "failed") throw new Error(job.error || "surveillance failed");
      setRes((job.result?.payload ?? null) as unknown as SignalOutput);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  }

  const sorted = res ? [...res.results].sort((a, b) => (b.prr ?? 0) - (a.prr ?? 0)) : [];

  return (
    <>
      <section className="hero" style={{ paddingBottom: 20 }}>
        <div className="eyebrow">Capability · pharmacovigilance</div>
        <h1 style={{ fontSize: 30 }}>Safety-Signal Surveillance</h1>
        <p>
          Disproportionality (PRR / ROR) over FAERS via <b>guarded text-to-SQL</b>. Your
          question becomes a read-only SELECT against a single view; a deny-by-default SQL
          guard validates it before it runs; you see the exact SQL that produced every
          number. PRR/ROR are screening signals — not confirmed causal risks.
        </p>
      </section>

      <div className="panel">
        <div className="eyebrow" style={{ marginBottom: 10 }}>Ask a signal question</div>
        <div className="chip-select">
          {SAMPLES.map((s) => (
            <button key={s} className={`dchip ${question === s ? "sel" : ""}`}
              onClick={() => setQuestion(s)}>
              <div className="dr" style={{ fontSize: 13 }}>{s}</div>
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <input value={question} onChange={(e) => setQuestion(e.target.value)}
            className="dchip" style={{ flex: 2, minWidth: 260, padding: "9px 12px" }}
            placeholder="signal question" />
          <input value={drug} onChange={(e) => setDrug(e.target.value)}
            className="dchip" style={{ flex: 1, minWidth: 130, padding: "9px 12px" }}
            placeholder="drug scope" />
          <input type="number" value={minCount} min={1}
            onChange={(e) => setMinCount(Number(e.target.value) || 1)}
            className="dchip" style={{ width: 110, padding: "9px 12px" }} title="min cell count a" />
          <button className="btn" onClick={run} disabled={running}>
            {running ? "Querying…" : "Run surveillance"}
          </button>
        </div>
        {running && <div className="note"><span className="spinner">text-to-SQL → guard → execute → summarise…</span></div>}
        {err && <div className="note" style={{ color: "var(--excluded)" }}>{err}</div>}
      </div>

      {res && (
        <>
          <div className="section-label">
            <span className="eyebrow">Generated SQL (exactly what executed)</span><span className="rule" />
          </div>
          <div className="panel">
            <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12,
              color: "var(--ink-soft)" }}>{res.generated_sql}</pre>
          </div>

          <div className="section-label">
            <span className="eyebrow">Disproportionality signals · sorted by PRR</span><span className="rule" />
          </div>
          <div className="panel" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {sorted.length === 0 && <span className="empty">No rows (try a lower min count or another drug).</span>}
            {sorted.map((r, i) => <Row key={i} r={r} />)}
          </div>

          <div className="boundary">
            <h4>Summary</h4>
            <p style={{ margin: "0 0 10px" }}>{res.summary || "—"}</p>
            <div className="eyebrow" style={{ marginBottom: 6 }}>Caveats</div>
            {res.caveats.map((c, i) => (
              <div className="kv" key={i}><span className="excluded-tag mono" style={{ fontSize: 12 }}>⚠ {c}</span></div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
