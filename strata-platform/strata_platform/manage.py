"""Operational CLI for the DB-backed deployment.

    python -m strata_platform.manage migrate          # alembic upgrade head (+ pgvector)
    python -m strata_platform.manage load-gold        # seed the SME gold table
    python -m strata_platform.manage ingest [--ids TA1156,TA1146] [--limit N]
    python -m strata_platform.manage eval   [--ids ...] [--out report.json]

``ingest`` gathers each decision's molecule-scoped, leakage-filtered public evidence,
runs the fail-loud health gate, embeds, and writes the pgvector corpus. ``eval`` runs the
capability closed- vs open-book over the SME gold decisions and prints the precision/recall
contrast (the finding). Both use the configured reasoner (GPT-5.x on Azure) + pgvector
store; locally they fall back to the offline KeywordReasoner + HashingEmbedder.
"""
from __future__ import annotations

import argparse
import json
import sys

from strata_platform.data import load_decisions as _bundled_decisions
from strata_platform.substrate.contracts import Decision


def _decisions_for(ids: list[str] | None, limit: int | None) -> list[Decision]:
    records = _bundled_decisions()
    decisions = [Decision.model_validate(r) for r in records]
    if ids:
        want = {i.upper() for i in ids}
        decisions = [d for d in decisions if d.decision_id.upper() in want]
    if limit:
        decisions = decisions[:limit]
    return decisions


def _gold_decisions(ids: list[str] | None, limit: int | None) -> list[Decision]:
    from strata_platform.data import load_sme_gold

    gold_ids = set(load_sme_gold())
    decisions = [d for d in _decisions_for(ids, None) if d.decision_id in gold_ids]
    return decisions[:limit] if limit else decisions


def cmd_migrate(_args) -> int:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    print("migrated to head (pgvector extension + tables + HNSW index)")
    return 0


def cmd_load_gold(_args) -> int:
    from strata_platform.db.gold import load_gold
    from strata_platform.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        n = load_gold(s)
    print(f"loaded {n} SME gold rows (annotator='sme')")
    return 0


def cmd_ingest(args) -> int:
    from strata_platform.config import get_settings
    from strata_platform.db.session import get_sessionmaker
    from strata_platform.ingest.corpus import build_corpus
    from strata_platform.ingest.health import CorpusHealthError, assert_corpus_healthy
    from strata_platform.ingest.pgvector import persist_chunks
    from strata_platform.sources.clinicaltrials import ClinicalTrialsClient
    from strata_platform.sources.nice_guidance import NICEGuidanceClient
    from strata_platform.sources.openfda import OpenFDAClient
    from strata_platform.sources.pubmed import PubMedClient

    s = get_settings()
    decisions = _gold_decisions(args.ids, args.limit)
    if not decisions:
        print("no decisions selected", file=sys.stderr)
        return 1
    print(f"ingesting {len(decisions)} decisions (buffer {s.retrieval_buffer_days}d)…")

    dossiers = []
    if args.dossiers:
        gc = NICEGuidanceClient()
        for d in decisions:
            try:
                gr = gc.fetch(d.decision_id)
                if gr.status == "ok":
                    dossiers.append(gr)
            except Exception as exc:  # noqa: BLE001 - dossier is optional context
                print(f"  guidance {d.decision_id}: {exc}", file=sys.stderr)

    chunks = build_corpus(decisions, ct=ClinicalTrialsClient(), pubmed=PubMedClient(),
                          fda=OpenFDAClient(), dossiers=dossiers,
                          buffer_days=s.retrieval_buffer_days)
    print(f"gathered {len(chunks)} chunks; running health gate…")
    try:
        report = assert_corpus_healthy(decisions, chunks,
                                       buffer_days=s.retrieval_buffer_days)
    except CorpusHealthError as exc:
        print(f"CORPUS REJECTED (not persisted): {exc}", file=sys.stderr)
        return 1
    print("health:", json.dumps(report, indent=2))
    written = persist_chunks(get_sessionmaker(), chunks)
    print(f"persisted {written} chunks to pgvector")
    return 0


def cmd_ingest_faers(args) -> int:
    from strata_platform.context.faers import ingest_faers
    from strata_platform.db.session import get_sessionmaker

    for drug in args.drugs:
        summary = ingest_faers(get_sessionmaker(), drug, limit=args.limit)
        print(json.dumps(summary))
    return 0


def cmd_eval(args) -> int:
    from strata_platform.db.gold import read_gold
    from strata_platform.db.session import get_sessionmaker
    from strata_platform.eval.hta_eval import run_hta_eval
    from strata_platform.substrate.reasoner import get_reasoner
    from strata_platform.substrate.store import get_store

    decisions = _gold_decisions(args.ids, args.limit)
    with get_sessionmaker()() as sess:
        gold = read_gold(sess, annotator="sme")
    decisions = [d for d in decisions if d.decision_id in gold]
    report = run_hta_eval(decisions, gold, reasoner=get_reasoner(), store=get_store())
    text = json.dumps(report, indent=2)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(text)
    print(text)
    d = report["delta"]
    print(f"\nHEADLINE  precision Δ={d['micro_precision']}  recall Δ={d['micro_recall']}",
          file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="strata.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("load-gold").set_defaults(fn=cmd_load_gold)

    pi = sub.add_parser("ingest")
    pi.add_argument("--ids", type=lambda s: s.split(","), default=None)
    pi.add_argument("--limit", type=int, default=None)
    pi.add_argument("--dossiers", action="store_true",
                    help="also fetch per-TA NICE guidance (network + WAF)")
    pi.set_defaults(fn=cmd_ingest)

    pf = sub.add_parser("ingest-faers")
    pf.add_argument("--drugs", type=lambda s: s.split(","), required=True)
    pf.add_argument("--limit", type=int, default=300)
    pf.set_defaults(fn=cmd_ingest_faers)

    pe = sub.add_parser("eval")
    pe.add_argument("--ids", type=lambda s: s.split(","), default=None)
    pe.add_argument("--limit", type=int, default=None)
    pe.add_argument("--out", default=None)
    pe.set_defaults(fn=cmd_eval)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
