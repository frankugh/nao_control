from __future__ import annotations

from cmdrec.rules import stop_rules


def test_stop_rules_match() -> None:
    assert stop_rules("stop ermee")
    assert stop_rules("halt alsjeblieft")
    assert stop_rules("ho nu")
    assert stop_rules("blijf staan")
    assert stop_rules("kap ermee")
    assert stop_rules("annuleer")
    assert stop_rules("niet doen")
    assert stop_rules("ga niet verder")
    assert stop_rules("niet verder lopen")
    assert stop_rules("pauze")
    assert stop_rules("pauzeer even")
    assert stop_rules("kut stop")
    assert stop_rules("shit stop")
    assert stop_rules("oeps stop")
    assert stop_rules("help stop")
    assert stop_rules("fuck, niet doen")
    assert stop_rules("kut, kappen nu")
    assert stop_rules("shit, kappen nu")
    assert stop_rules("ho nee")
    assert stop_rules("ophouden")
    assert stop_rules("ophouden nu")
    assert stop_rules("kappen")
    assert stop_rules("kappen nu")
    assert stop_rules("kappen, snel nu")
    assert not stop_rules("ga verder")


def test_stop_rules_no_substring_match() -> None:
    assert not stop_rules("stoomboot")
    assert not stop_rules("desktop")
    assert not stop_rules("stopcontact")
    assert not stop_rules("wachttijd")
    assert not stop_rules("ik wachtte gisteren lang")
    assert not stop_rules("wat doe je als ik stop zeg")
    assert not stop_rules("kut.")
    assert not stop_rules("shit.")
    assert not stop_rules("oeps")
    assert not stop_rules("help")
    assert not stop_rules("ik zei kut gisteren")
    assert not stop_rules("dit is shit nieuws")
    assert not stop_rules("ho, dat klinkt ouderwets.")
    assert not stop_rules('ik bedoelde "blijf staan" als grapje.')
