"""Tests for the classifier, concentrated on the ways keyword matching fails.

Every false-positive mode here inflates apparent Medicare for All support, so
these are the tests that keep the headline number honest.
"""

import pytest

from dcp.classify.classifier import classify_pages, classify_text, extract_text
from dcp.classify.taxonomy import Stance, detect_stance
from dcp.models import M4ATier

# Padding to clear the minimum-length guard without affecting classification.
PAD = (
    " Our district deserves a representative who shows up and does the work every"
    " single day, listens to neighbors, and answers to the people who sent them."
)


def tier(text: str) -> M4ATier:
    return classify_text(text + PAD, "http://example.org").tier


# --- stance detection ------------------------------------------------------

@pytest.mark.parametrize("sentence,needle,expected", [
    ("I support Medicare for All.", "Medicare for All", Stance.AFFIRM),
    ("I do not support Medicare for All.", "Medicare for All", Stance.NEGATE),
    ("I oppose Medicare for All.", "Medicare for All", Stance.NEGATE),
    ("My opponent supports Medicare for All.", "Medicare for All", Stance.ATTRIBUTED),
    ("Republicans want to repeal the Affordable Care Act.", "Affordable Care Act", Stance.ATTRIBUTED),
    ("I support a public option rather than Medicare for All.", "Medicare for All", Stance.NEGATE),
])
def test_detect_stance(sentence, needle, expected):
    i = sentence.index(needle)
    assert detect_stance(sentence, i, i + len(needle)) is expected


# --- tier assignment -------------------------------------------------------

def test_explicit_endorsement():
    assert tier("I support Medicare for All and will fight to pass it.") is M4ATier.EXPLICIT_M4A


def test_single_payer_by_name_is_explicit():
    assert tier("I believe in a single-payer health care system for this country.") is M4ATier.EXPLICIT_M4A


def test_single_payer_in_substance_without_the_brand():
    text = (
        "We must replace private insurance with one public plan that covers every "
        "American, with no premiums or deductibles."
    )
    assert tier(text) is M4ATier.SINGLE_PAYER_SUBSTANCE


def test_public_option_is_not_counted_as_m4a():
    text = "I support a public option so every family can buy into Medicare."
    assert tier(text) is M4ATier.PUBLIC_OPTION


def test_protecting_medicare_is_not_medicare_for_all():
    # The single most common false positive: near-universal Democratic boilerplate.
    text = "I will always protect and strengthen Medicare for our seniors."
    assert tier(text) is M4ATier.ACA_STRENGTHEN


def test_aca_only_position():
    text = (
        "I will defend the Affordable Care Act, protect coverage for pre-existing "
        "conditions, and let Medicare negotiate prescription drug prices."
    )
    assert tier(text) is M4ATier.ACA_STRENGTHEN


def test_no_healthcare_content():
    text = (
        "I am running for Congress to fight for working families. I grew up here, "
        "went to school here, and I want good jobs and strong schools."
    )
    assert tier(text) is M4ATier.NO_HEALTHCARE_POSITION


# --- the false-positive traps ---------------------------------------------

def test_negated_endorsement_is_not_support():
    result = classify_text(
        "I do not support Medicare for All. I back a public option instead." + PAD,
        "http://example.org",
    )
    assert result.tier is M4ATier.PUBLIC_OPTION
    assert result.explicitly_rejects_m4a


def test_position_attributed_to_opponent_is_not_the_candidates():
    result = classify_text(
        "My opponent supports Medicare for All, which he calls a moral necessity." + PAD,
        "http://example.org",
    )
    assert result.tier is not M4ATier.EXPLICIT_M4A


def test_third_party_endorsement_quote_does_not_set_the_tier():
    result = classify_text(
        "According to their statement, she is a longtime champion of Medicare for All." + PAD,
        "http://example.org",
    )
    assert result.tier is not M4ATier.EXPLICIT_M4A
    assert result.needs_review


def test_hedged_admiration_is_not_endorsement():
    result = classify_text(
        "While I admire the goal of Medicare for All, we should start with a public option." + PAD,
        "http://example.org",
    )
    assert result.tier is not M4ATier.EXPLICIT_M4A


def test_explicit_opposition():
    result = classify_text(
        "Medicare for All is a government takeover of health care and I will stop it." + PAD,
        "http://example.org",
    )
    assert result.tier is M4ATier.OPPOSED


# --- evidence and multi-page behaviour ------------------------------------

def test_classification_carries_a_verbatim_quote():
    result = classify_text(
        "I proudly support Medicare for All." + PAD, "http://example.org/issues"
    )
    assert result.evidence
    ev = result.evidence[0]
    assert "Medicare for All" in ev.quote
    assert ev.url == "http://example.org/issues"
    assert ev.matched_rule == "m4a.phrase"


def test_multi_page_keeps_the_strongest_position():
    pages = {
        "http://x/": "<html><body><p>Welcome to the campaign. Donate today." + PAD + "</p></body></html>",
        "http://x/issues": "<html><body><p>I will protect the Affordable Care Act." + PAD + "</p></body></html>",
        "http://x/health": "<html><body><p>I support Medicare for All and will co-sponsor it." + PAD + "</p></body></html>",
    }
    assert classify_pages(pages).tier is M4ATier.EXPLICIT_M4A


def test_no_pages_is_unknown_not_no_position():
    result = classify_pages({})
    assert result.tier is M4ATier.UNKNOWN
    assert result.needs_review


def test_extract_text_drops_nav_and_scripts():
    html = """
    <html><body>
      <nav><a href="/donate">Donate</a> Medicare for All</nav>
      <script>var x = "Medicare for All";</script>
      <main><p>I support a public option.</p></main>
    </body></html>
    """
    text = extract_text(html)
    assert "Medicare for All" not in text
    assert "public option" in text
