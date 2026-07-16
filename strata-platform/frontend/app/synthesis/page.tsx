"use client";

import { useEffect, useState } from "react";
import ContextPanel, { type ContextMode } from "../ContextPanel";
import {
  api,
  runToCompletion,
  type Decision,
  type EvidenceClaim,
  type NarrativeParagraph,
  type SynthesisResult,
} from "../../lib/api";

const DIM: Record<string, string> = {
  efficacy: "Efficacy",
  comparator: "Comparator",
  safety: "Safety",
  economic: "Economic",
  generalizability: "Generalisability",
  other: "Other",
};

function ClaimRow({ c }: { c: EvidenceClaim }) {
  const [open, setOpen] = useState(false);
  const srcs = c.provenance?.source_ids ?? [];
  return (
    <div className={`vchip ${c.support === "partial" ? "parametric" : "grounded"}`}>
      <span className="cat">
        {c.support === "partial" && <span title="partially supported"> ● </span>}
        {c.text}
      </span>
      {srcs.length > 0 && (
        <>
          <div className="prov-toggle" onClick={() => setOpen(!open)}>
            {open ? "▾ hide sources" : `▸ ${srcs.length} source${srcs.length > 1 ? "s" : ""}`}
          </div>
          {open && (
            <div className="prov">
              {srcs.map((s, i) => <div className="src" key={i}>{s}</div>)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Para({ p, claims }: { p: NarrativeParagraph; claims: EvidenceClaim[] }) {
  const [open, setOpen] = useState(false);
  const byId = Object.fromEntries(claims.map((c) => [c.claim_id, c]));
  return (
    <div style={{ marginBottom: 14 }}>
      <p style={{ margin: "0 0 4px" }}>{p.text}</p>
      <div className="prov-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾ hide grounding" : `▸ grounded in ${p.claim_ids.length} claim${p.claim_ids.length > 1 ? "s" : ""}`}
      </div>
      {open && (
        <div className="prov">
          {p.claim_ids.map((id) => {
            const c = byId[id];
            return (
              <div className="src" key={id}>
                {c ? `${c.text} - [${(c.provenance?.source_ids ?? []).join(", ")}]` : id}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function Synthesis() {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [sel, setSel] = useState<Decision | null>(null);
  const [res, setRes] = useState<SynthesisResult | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ctx, setCtx] = useState<ContextMode>({ boundary_mode: "backtest" });

  useEffect(() => {
    api.samples()
      .then((r) => { setDecisions(r.decisions); setSel(r.decisions[0] ?? null); })
      .catch(() => setErr("Can't reach the platform API."));
  }, []);

  async function run() {
    if (!sel) return;
    setRunning(true); setErr(null); setRes(null);
    try {
      await api.seed();
      const job = await runToCompletion("evidence_synthesis", sel,
        { boundary_mode: ctx.boundary_mode, as_of: ctx.as_of }, 120000);
      if (job.status === "failed") throw new Error(job.error || "synthesis failed");
      setRes((job.result?.payload ?? null) as unknown as SynthesisResult);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  }

  const allClaims = res ? res.brief.flatMap((d) => d.claims) : [];
  const b = (res?.boundary ?? null) as
    | { cutoff?: string; buffer_days?: number; molecules?: string[]; mode?: string }
    | null;

  return (
    <>
      <section className="hero" style={{ paddingBottom: 20 }}>
        <div className="eyebrow">Capability · grounded generator</div>
        <h1 style={{ fontSize: 30 }}>Evidence Synthesis</h1>
        <p>
          A dossier-style evidence brief composed only from grounded claims. Every claim
          is checked against its source by an automated groundedness gate; unsupported
          claims are dropped. Every sentence of the narrative traces back to a claim, and
          every claim to a retrieved source.
        </p>
      </section>

      <ContextPanel decision={sel} onMode={setCtx} />

      <div className="panel">
        <div className="eyebrow" style={{ marginBottom: 10 }}>Choose a decision</div>
        <div className="chip-select">
          {decisions.map((d) => (
            <button key={d.decision_id}
              className={`dchip ${sel?.decision_id === d.decision_id ? "sel" : ""}`}
              onClick={() => setSel(d)}>
              <div className="id">{d.decision_id} · {d.decision_date}</div>
              <div className="dr">{d.drug}</div>
              <div className="ind">{d.indication}</div>
            </button>
          ))}
        </div>
        <div style={{ marginTop: 16 }}>
          <button className="btn" onClick={run} disabled={running || !sel}>
            {running ? "Synthesising…" : "Run synthesis"}
          </button>
          {running && <span className="spinner" style={{ marginLeft: 12 }}>extract -> gate -> compose…</span>}
        </div>
        {err && <div className="note" style={{ color: "var(--excluded)" }}>{err}</div>}
      </div>

      {res && (
        <>
          <div className="boundary" style={{ display: "flex", gap: 24, alignItems: "center" }}>
            <div>
              <div style={{ fontSize: 30, fontWeight: 700, color: "var(--grounded)" }}>
                {Math.round(res.groundedness_score * 100)}%
              </div>
              <div className="eyebrow">groundedness</div>
            </div>
            <div className="note" style={{ margin: 0 }}>
              {res.filtered_claims.length} claim{res.filtered_claims.length === 1 ? "" : "s"} dropped
              as unsupported by the gate · {res.retrieved_chunks} chunks retrieved within boundary.
              <br />A dropped claim is a feature: it proves the gate refuses ungrounded text.
            </div>
          </div>

          <div className="section-label">
            <span className="eyebrow">Structured evidence brief</span><span className="rule" />
          </div>
          {res.brief.map((dim) => (
            <div className="panel" key={dim.dimension} style={{ marginBottom: 14 }}>
              <div className="eyebrow" style={{ marginBottom: 10 }}>{DIM[dim.dimension] ?? dim.dimension}</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {dim.claims.map((c) => <ClaimRow key={c.claim_id} c={c} />)}
              </div>
            </div>
          ))}

          <div className="section-label">
            <span className="eyebrow">Dossier narrative</span><span className="rule" />
          </div>
          <div className="panel">
            {res.narrative.length === 0 && <span className="empty">No grounded narrative produced.</span>}
            {res.narrative.map((p, i) => <Para key={i} p={p} claims={allClaims} />)}
          </div>

          {b && (
            <div className="boundary">
              <h4>Leakage boundary enforced ({b.mode})</h4>
              <div className="kv"><span className="k">retrieval cutoff</span>
                <span className="mono">{b.cutoff}</span></div>
              <div className="kv"><span className="k">molecule scope</span>
                <span className="mono">{(b.molecules ?? []).join(", ")}</span></div>
              <div className="kv"><span className="k">evidence retrieved</span>
                <span className="mono">{res.retrieved_chunks} chunks within boundary</span></div>
            </div>
          )}
        </>
      )}
    </>
  );
}
