from __future__ import annotations

from signalpost import planner


def test_shell_company_is_t0():
    row = {
        "organisasjonsform.kode": "BRL", "antallAnsatte": "", "harRegistrertAntallAnsatte": "false",
        "naeringskode1.kode": "68.209", "hjemmeside": "", "epostadresse": "", "konkurs": "false", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier == "T0"
    assert result.allowance == 5


def test_small_staffed_company_is_t1():
    row = {
        "organisasjonsform.kode": "AS", "antallAnsatte": "3", "harRegistrertAntallAnsatte": "true",
        "naeringskode1.kode": "43.210", "hjemmeside": "", "epostadresse": "", "konkurs": "false", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier == "T1"


def test_no_staff_with_website_is_t1():
    row = {
        "organisasjonsform.kode": "AS", "antallAnsatte": "", "harRegistrertAntallAnsatte": "false",
        "naeringskode1.kode": "43.210", "hjemmeside": "example.no", "epostadresse": "", "konkurs": "false", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier == "T1"


def test_mid_staffed_company_is_t2():
    row = {
        "organisasjonsform.kode": "AS", "antallAnsatte": "20", "harRegistrertAntallAnsatte": "true",
        "naeringskode1.kode": "62.200", "hjemmeside": "example.no", "epostadresse": "", "konkurs": "false", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier == "T2"
    assert result.allowance == 30


def test_large_company_is_t3():
    row = {
        "organisasjonsform.kode": "AS", "antallAnsatte": "352", "harRegistrertAntallAnsatte": "true",
        "naeringskode1.kode": "62.200", "hjemmeside": "www.dips.com", "epostadresse": "", "konkurs": "false", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier == "T3"
    assert result.allowance == 45


def test_bankrupt_company_is_capped_down():
    row = {
        "organisasjonsform.kode": "AS", "antallAnsatte": "20", "harRegistrertAntallAnsatte": "true",
        "naeringskode1.kode": "62.200", "hjemmeside": "example.no", "epostadresse": "", "konkurs": "true", "underAvvikling": "false",
    }
    result = planner.classify(row)
    assert result.tier in ("T0", "T1")
