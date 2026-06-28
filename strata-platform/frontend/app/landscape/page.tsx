"use client";

import { useState } from "react";
import {
  runParamsToCompletion,
  type ComparatorEntry,
  type EndpointEntry,
  type LandscapeResult,
} from "../../lib/api";

const SAMPLES = [
  "EGFR-mutated non-small-cell lung cancer",
  "relapsed or refractory multiple myeloma",
  "advanced renal cell carcinoma",
  "HER2-positive breast cancer",
];

const HTA_COLOR: Record<string, string> = {
  accepted: "var(--grounded)",
  contested: "var(--parametric)",
  rejected: "var(--excluded)",
  unknown: "var(--ink-soft)",
};

function Prov({ ids, label }: { ids: string[]; label: string }) {
  const [open, setOpen] = useState(false);
  if (!ids.length) return null;
  return (
    <>
      <div className="prov-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾ hide" : `▸ ${ids.length} ${label}`}
      </div>
      {open && <div className="prov">{ids.map((s) => <div className="src" key={s}>{s}</div>)}</div>}
    </>
  );
}

function EndpointRow({ e }: { e: EndpointEntry }) {
  return (
    <div className="vchip grounded">
      <span className="cat">
        {e.canonical}{" "}
        <span className="badge preview" style={{ marginLeft: 6 }}>{e.kind}</span>
        {e.is_surrogate && (
          <span className="badge" style={{ background: "var(--parametric-bg)", color: "var(--parametric)", marginLeft: 6 }}>
            surrogate
          </span>
        )}
        <span className="mono" style={{ marginLeft: 8, color: "var(--ink-soft)" }}>
          {e.trial_count} trial{e.trial_count > 1 ? "s" : ""}
        </span>
      </span>
      <Prov ids={e.provenance.source_ids} label="trials" />
    </div>
  );
}

function ComparatorRow({ c }: { c: ComparatorEntry }) {
  return (
    <div className="vchip grounded">
      <span className="cat">
        {c.canonical}
        {c.comparator_class && (
          <span className="mono" style={{ marginLeft: 8, color: "var(--ink-soft)" }}>
            {c.comparator_class}
          </span>
        )}
        <span className="badge" style={{ marginLeft: 6, background: "transparent", color: HTA_COLOR[c.hta_signal], border: `1px solid ${HTA_COLOR[c.hta_signal]}` }}>
          NICE: {c.hta_signal}
        </span>
        <span className="mono" style={{ marginLeft: 8, color: "var(--ink-soft)" }}>
          {c.trial_count} trial{c.trial_count > 1 ? "s" : ""}
        </span>
      </span>
      <Prov ids={c.provenance.source_ids} label="trials" />
    </div>
  );
}

export default function Landscape() {
  const [indication, setIndication] = useState(SAMPLES[0]);
  const [drug, setDrug] = useState("");
  const [res, setRes] = useState<LandscapeResult | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function build() {
    if (!indication.trim()) return;
    setRunning(true); setErr(null); setRes(null);
    try {
      const params: Record<string, unknown> = { indication };
      if (drug.trim()) params.drug = drug.trim();
      const job = await runParamsToCompletion("endpoint_landscape", params, 180000);
      if (job.status === "failed") throw new Error(job.error || "landscape failed");
      setRes((job.result?.payload ?? null) as unknown as LandscapeResult);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <section className="hero" style={{ paddingBottom: 20 }}>
        <div className="eyebrow">Capability · indication-centric</div>
        <h1 style={{ fontSize: 30 }}>Endpoint &amp; Comparator Landscape</h1>
        <p>
          For an indication, reconstruct the endpoints and comparators that registered
          trials have used — to inform trial design before submission. Counts are
          deterministic from the ClinicalTrials.gov structured fields; the model only
          clusters variant names and flags surrogates. Every entry traces to its NCT ids.
        </p>
      </section>

      <div className="panel">
        <div className="eyebrow" style={{ marginBottom: 10 }}>Indication</div>
        <div className="chip-select">
          {SAMPLES.map((s) => (
            <button key={s} className={`dchip ${indication === s ? "sel" : ""}`}
              onClick={() => setIndication(s)}>
              <div className="dr" style={{ fontSize: 13 }}>{s}</div>
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <input value={indication} onChange={(e) => setIndication(e.target.value)}
            className="dchip" style={{ flex: 2, minWidth: 240, padding: "9px 12px" }}
            placeholder="indication" />
          <input value={drug} onChange={(e) => setDrug(e.target.value)}
            className="dchip" style={{ flex: 1, minWidth: 140, padding: "9px 12px" }}
            placeholder="narrow by drug (optional)" />
          <button className="btn" onClick={build} disabled={running}>
            {running ? "Building landscape…" : "Build landscape"}
          </button>
        </div>
        {running && <div className="note"><span className="spinner">fetching ClinicalTrials.gov + clustering…</span></div>}
        {err && <div className="note" style={{ color: "var(--excluded)" }}>{err}</div>}
      </div>

      {res && (
        <>
          <div className="note" style={{ marginTop: 14 }}>
            {res.trials_analysed} trials analysed for <b>{res.indication}</b> as-of {res.as_of}.
          </div>

          <div className="section-label">
            <span className="eyebrow">Endpoints{res.endpoints.length > 40 ? ` · top 40 of ${res.endpoints.length}` : ""}</span>
            <span className="rule" />
          </div>
          <div className="panel" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {res.endpoints.length === 0 && <span className="empty">No endpoints found.</span>}
            {res.endpoints.slice(0, 40).map((e) => <EndpointRow key={e.entry_id} e={e} />)}
          </div>

          <div className="section-label">
            <span className="eyebrow">Comparators</span><span className="rule" />
          </div>
          <div className="panel" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {res.comparators.length === 0 && <span className="empty">No comparator arms found.</span>}
            {res.comparators.map((c) => <ComparatorRow key={c.entry_id} c={c} />)}
          </div>

          <div className="section-label">
            <span className="eyebrow">Design implications</span><span className="rule" />
          </div>
          <div className="panel">
            {res.implications.length === 0 && <span className="empty">—</span>}
            {res.implications.map((im, i) => (
              <div key={i} style={{ marginBottom: 10 }}>
                <p style={{ margin: "0 0 2px" }}>{im.text}</p>
                <div className="mono" style={{ fontSize: 11, color: "var(--ink-soft)" }}>
                  grounded in: {im.refs.join(", ")}
                </div>
              </div>
            ))}
          </div>

          <div className="boundary">
            <h4>Live landscape ({(res.boundary as { mode?: string }).mode})</h4>
            <div className="kv"><span className="k">as-of</span>
              <span className="mono">{String((res.boundary as { as_of?: string }).as_of)}</span></div>
            <div className="note">
              Deterministic counts from the registry; the model only clusters names and flags
              surrogates (lexicon-first). Click any entry to reach the trials behind it.
            </div>
          </div>
        </>
      )}
    </>
  );
}
