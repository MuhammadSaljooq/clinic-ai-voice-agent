"""FAQ matching.

The property that matters most is refusal. Answering "my chest hurts" with the parking
answer is far worse than admitting we do not know and handing the caller to a person.
"""

from __future__ import annotations

import pathlib

import pytest

from clinic_agent.agent.faq import match_faq
from clinic_agent.config import load_config

REPO = pathlib.Path(__file__).resolve().parents[1]
FAQ = load_config(REPO / "config.yaml").faq


def answer_for(question: str) -> str | None:
    entry = match_faq(question, FAQ)
    return entry.a if entry else None


def test_an_exact_question_matches():
    assert "Northside Avenue" in answer_for("Where are you located?")


def test_a_paraphrase_matches():
    assert "Northside Avenue" in answer_for("whereabouts are you located")


def test_matching_ignores_case_and_punctuation():
    assert "Northside Avenue" in answer_for("WHERE ARE YOU LOCATED???")


def test_plural_and_singular_both_match():
    assert answer_for("what are your hours") is not None
    assert answer_for("what is your hour") is not None


def test_a_differently_worded_question_still_finds_the_right_entry():
    assert "insurance card" in answer_for("what do I need to bring with me")


@pytest.mark.parametrize(
    "question",
    [
        "my chest hurts really badly",
        "do I need a referral from my cardiologist",
        "can you tell me my test results",
        "how much does an MRI cost",
    ],
)
def test_questions_we_cannot_answer_are_refused_not_guessed(question):
    """A wrong confident answer from a clinic phone line is a real harm."""
    assert match_faq(question, FAQ) is None


def test_an_empty_question_is_refused():
    assert match_faq("", FAQ) is None
    assert match_faq("   ?? ", FAQ) is None


def test_a_question_of_only_filler_words_is_refused():
    assert match_faq("um so like the a of and", FAQ) is None


def test_no_configured_faq_means_no_answer():
    assert match_faq("where are you located", []) is None
