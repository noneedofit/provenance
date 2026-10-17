from signalpost.evidence_text import raw_json_quote

ENTITY = b'{"navn":"GEMMA AS","forretningsadresse":{"adresse":["Heiane 2B"],"postnummer":"5131","poststed":"NYBORG"},"konkurs":false}'
ACCOUNTS = (b'[{"id":1,"resultatregnskapResultat":{"aarsresultat":-5}},'
            b'{"id":7182437,"resultatregnskapResultat":{"aarsresultat":-147521,"driftsresultat":{"driftsinntekter":{"sumDriftsinntekter":35247070}}}}]')
ROLES = (b'{"rollegrupper":[{"roller":[{"avregistrert":false,"person":{"erDoed":false,"fodselsdato":"1964-12-21",'
         b'"navn":{"etternavn":"Stokkeholm","fornavn":"Tone Kristin"}},"type":{"kode":"DAGL"}},'
         b'{"enhet":{"navn":["KPMG AS"],"organisasjonsnummer":"935174627"},"type":{"kode":"REVI"}}],"type":{"kode":"DAGL"}}]}')


def test_quote_is_copied_verbatim_from_the_response():
    for body, path in ((ENTITY, "$.forretningsadresse"), (ENTITY, "$.konkurs"), (ACCOUNTS, "$[id=7182437].resultatregnskapResultat.aarsresultat")):
        quote = raw_json_quote(body, path)
        assert quote and quote in body.decode()
    assert raw_json_quote(ENTITY, "$.forretningsadresse") == '"forretningsadresse":{"adresse":["Heiane 2B"],"postnummer":"5131","poststed":"NYBORG"}'
    # The record is picked by id, not position: the other record's result is not quoted.
    assert raw_json_quote(ACCOUNTS, "$[id=7182437].resultatregnskapResultat.aarsresultat") == '"aarsresultat":-147521'
    assert raw_json_quote(ACCOUNTS, "$[id=7182437].resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter") == '"sumDriftsinntekter":35247070'


def test_role_quote_never_includes_a_birth_date():
    person = raw_json_quote(ROLES, "$.rollegrupper[type.kode=DAGL].roller[0]")
    assert person == '"navn":{"etternavn":"Stokkeholm","fornavn":"Tone Kristin"}'
    entity = raw_json_quote(ROLES, "$.rollegrupper[type.kode=DAGL].roller[1]")
    assert "KPMG AS" in entity and entity in ROLES.decode()


def test_unresolvable_paths_give_no_quote():
    assert raw_json_quote(ENTITY, "$.hjemmeside") is None
    assert raw_json_quote(ENTITY, "$.konkurs/underAvvikling") is None
    assert raw_json_quote(b"not json", "$.navn") is None
    assert raw_json_quote(None, "$.navn") is None
    assert raw_json_quote(ENTITY, "row organisasjonsnummer=1") is None
