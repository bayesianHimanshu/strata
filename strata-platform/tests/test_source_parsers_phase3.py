"""Pure parsers for the retrieval corpus: PubMed efetch + openFDA labels (no network)."""
from __future__ import annotations

from datetime import date

from strata_platform.sources.openfda import parse_label_docs
from strata_platform.sources.pubmed import parse_efetch

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40000001</PMID>
      <Article>
        <ArticleTitle>Overall survival was immature in the trial</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">The comparator was debated.</AbstractText>
          <AbstractText Label="RESULTS">OS data were immature.</AbstractText>
        </Abstract>
        <Journal><JournalIssue><PubDate>
          <Year>2024</Year><Month>Mar</Month><Day>15</Day>
        </PubDate></JournalIssue></Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>40000002</PMID>
      <Article>
        <ArticleTitle>Cost-effectiveness analysis</ArticleTitle>
        <Abstract><AbstractText>ICER was uncertain.</AbstractText></Abstract>
        <Journal><JournalIssue><PubDate>
          <MedlineDate>2023 Winter</MedlineDate>
        </PubDate></JournalIssue></Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


def test_parse_efetch_extracts_title_abstract_date() -> None:
    out = parse_efetch(EFETCH_XML)
    assert len(out) == 2
    a = out[0]
    assert a.pmid == "40000001"
    assert "immature" in a.title.lower()
    assert "comparator" in a.abstract.lower() and "immature" in a.abstract.lower()
    assert a.pub_date == date(2024, 3, 15)


def test_parse_efetch_medline_date_fallback() -> None:
    out = parse_efetch(EFETCH_XML)
    assert out[1].pub_date == date(2023, 1, 1)


def test_parse_efetch_bad_xml_is_empty() -> None:
    assert parse_efetch("<not-xml") == []


def test_parse_label_docs_text_and_date() -> None:
    payload = {
        "results": [
            {
                "openfda": {
                    "brand_name": ["Keytruda"],
                    "generic_name": ["pembrolizumab"],
                },
                "indications_and_usage": ["KEYTRUDA is indicated for NSCLC."],
                "warnings_and_cautions": ["Immune-mediated adverse reactions."],
                "effective_time": "20240315",
            },
            {"openfda": {}, "effective_time": "20240101"},  # no text -> dropped
        ]
    }
    docs = parse_label_docs(payload)
    assert len(docs) == 1
    d = docs[0]
    assert d.brand == "Keytruda" and d.generic == "pembrolizumab"
    assert "NSCLC" in d.text and "Immune-mediated" in d.text
    assert d.effective_date == date(2024, 3, 15)


def test_parse_label_docs_empty() -> None:
    assert parse_label_docs({"results": []}) == []
