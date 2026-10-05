from signalpost import evidence_text
from signalpost.models import Claim, Envelope, Evidence, RunInfo, claim_key


def _ev(eid, span, method="brreg_entity_v1"):
    return Evidence(evidence_id=eid, source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123456785",
                    source_class="official_registry", retrieved_at="2026-10-05T00:00:00Z", extraction_method=method, span=span)


def _claim(family, field, value, eid, value_key=None):
    return Claim(claim_id=claim_key("123456785", family, field, value_key), organisation_number="123456785",
                 family=family, field=field, value=value, value_key=value_key, availability="available",
                 identity_basis="registry_record", evidence_ids=[eid])


def test_every_evidence_gets_id_and_exact_supporting_text():
    env = Envelope(organisation_number="123456785", run=RunInfo(run_id="r", started_at="2026-10-05T00:00:00Z"),
                   claims=[
                       _claim("identity", "legal_name", "ACME AS", "e1"),
                       _claim("financials", "revenue", {"amount": 246367.0, "currency": "NOK"}, "e2"),
                       _claim("website", "official_website", {"url": "https://acme.no/", "domain": "acme.no"}, "e3"),
                   ],
                   evidence=[_ev("e1", "$.navn"), _ev("e2", "$[id=1].sumDriftsinntekter"),
                             _ev("e3", "Org nr. 123 456 785", "web_org_number_on_source_v1")])
    out = evidence_text.complete(env)
    spans = {e.evidence_id: e.claim_span for e in out.evidence}
    assert spans == {"e1": "ACME AS", "e2": "246367 NOK", "e3": "Org nr. 123 456 785"}
    assert all(e.id == e.evidence_id for e in out.evidence)


def test_output_order_is_deterministic():
    claims = [_claim("website", "official_website", "https://a.no/", "e1"), _claim("identity", "legal_name", "A AS", "e2")]
    evs = [_ev("e2", "$.navn"), _ev("e1", "x")]
    a = evidence_text.complete(Envelope(organisation_number="123456785", run=RunInfo(run_id="r", started_at="t"), claims=list(claims), evidence=list(evs)))
    b = evidence_text.complete(Envelope(organisation_number="123456785", run=RunInfo(run_id="r", started_at="t"), claims=list(reversed(claims)), evidence=list(reversed(evs))))
    assert [c.claim_id for c in a.claims] == [c.claim_id for c in b.claims]
    assert [e.evidence_id for e in a.evidence] == [e.evidence_id for e in b.evidence]
