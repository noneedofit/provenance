import json

from signalpost.registry import _ClaimBuilder, group_structure_claims

TREE = {"children": [{"children": [{"dato": "2025-09-08", "grunnlag": "100%", "knytningsform": {"beskrivelse": "Konsern datter", "kode": "KDAT"},
                                    "navn": "CHILD AS", "nivaa": 2, "organisasjonsform": {"kode": "AS"}, "organisasjonsnummer": "333333333",
                                    "parentNavn": "MIDDLE AS", "parentOrganisasjonsnummer": "222222222"}],
                      "dato": "2026-09-11", "grunnlag": "68,53%", "knytningsform": {"beskrivelse": "Konsern datter", "kode": "KDAT"},
                      "navn": "MIDDLE AS", "nivaa": 1, "organisasjonsform": {"kode": "AS"}, "organisasjonsnummer": "222222222",
                      "parentNavn": "TOP ASA", "parentOrganisasjonsnummer": "111111111"}],
        "navn": "TOP ASA", "organisasjonsform": {"kode": "ASA"}, "organisasjonsnummer": "111111111"}


class Resp:
    url = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/222222222"
    final_url = url
    redirect_chain = [url]
    status = 200
    retrieved_at = "2026-10-19T00:00:00Z"
    content_sha256 = "x"
    snapshot_ref = None
    body = json.dumps(TREE, ensure_ascii=False, separators=(",", ":")).encode()


def test_middle_company_gets_parent_and_subsidiary_with_verbatim_quotes():
    builder = _ClaimBuilder("222222222", "2026-10-19T00:00:00Z")
    group_structure_claims(builder, TREE, Resp(), "222222222")
    by_field = {c.field: c for c in builder.claims}
    assert by_field["parent_company"].value["organisation_number"] == "111111111"
    assert by_field["parent_company"].value["ownership"] == "68,53%"
    assert by_field["subsidiary"].value["name"] == "CHILD AS"
    assert "ultimate_parent" not in by_field  # the direct parent is the top of the group
    raw = Resp.body.decode()
    for ev in builder.evidence.values():
        assert ev.claim_span and ev.claim_span in raw
        assert "CHILD AS" not in ev.claim_span or "MIDDLE AS" in ev.claim_span


def test_grandchild_gets_ultimate_parent():
    builder = _ClaimBuilder("333333333", "2026-10-19T00:00:00Z")
    group_structure_claims(builder, TREE, Resp(), "333333333")
    fields = {c.field: c.value for c in builder.claims}
    assert fields["parent_company"]["name"] == "MIDDLE AS"
    assert fields["ultimate_parent"] == {"name": "TOP ASA", "organisation_number": "111111111"}
