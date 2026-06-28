"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Capability } from "../lib/api";

const COPY: Record<string, { title: string; live: boolean; href?: string; badge?: string }> = {
  hta_archaeology: { title: "HTA Archaeology", live: true, href: "/hta" },
  endpoint_landscape: {
    title: "Endpoint & Comparator Landscape", live: true, href: "/landscape", badge: "live",
  },
  evidence_synthesis: {
    title: "Evidence Synthesis", live: true, href: "/synthesis", badge: "grounded",
  },
  safety_surveillance: {
    title: "Safety-Signal Surveillance", live: true, href: "/surveillance", badge: "live",
  },
};

export default function Home() {
  const [caps, setCaps] = useState<Capability[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.capabilities().then((r) => setCaps(r.capabilities)).catch(() =>
      setErr("Can't reach the platform API. Set NEXT_PUBLIC_API_BASE and confirm the service is running."),
    );
  }, []);

  return (
    <>
      <section className="hero">
        <div className="eyebrow">Integrated Evidence Generation · public data only</div>
        <h1>Anticipate the evidence, before the committee does.</h1>
        <p>
          STRATA runs evidence-generation capabilities on a trust substrate: every claim
          is grounded in retrieved sources, under a leakage boundary enforced in code.
          You see the reasoning and its provenance — not a black box.
        </p>
        <div className="finding">
          Validated finding: grounding a frontier model in public evidence turns an
          over-confident prior into a <b>disciplined, higher-precision</b> predictor of
          HTA evidence concerns — most decisively for cost-effectiveness uncertainty
          (precision 0.44&nbsp;→&nbsp;1.00) — and stays honest where the public record
          is silent.
        </div>
      </section>

      <div className="section-label">
        <span className="eyebrow">Capabilities</span>
        <span className="rule" />
      </div>

      {err && <div className="panel empty">{err}</div>}

      <div className="grid">
        {caps.map((c) => {
          const meta = COPY[c.key] || { title: c.key, live: false };
          const badge = meta.badge ?? (meta.live ? "validated" : "preview");
          const inner = (
            <div className={`card ${meta.live ? "live" : ""}`}>
              <div className="k">
                <h3>{meta.title}</h3>
                <span className={`badge ${meta.live ? "validated" : "preview"}`}>
                  {badge}
                </span>
              </div>
              <p>{c.summary}</p>
              {meta.live && <span className="go">Open the analysis →</span>}
            </div>
          );
          return meta.live && meta.href ? (
            <Link key={c.key} href={meta.href}>{inner}</Link>
          ) : (
            <div key={c.key}>{inner}</div>
          );
        })}
      </div>
    </>
  );
}
