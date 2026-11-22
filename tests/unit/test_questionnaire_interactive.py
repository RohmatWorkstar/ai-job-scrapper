"""Drive the real Questionary prompts with simulated key presses."""

from __future__ import annotations

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from src.cli.questions import ask_questions, build_search_config, default_answers
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv


def _run(settings, profile, keys: str) -> dict:
    defaults = default_answers(settings, None, profile)
    with create_pipe_input() as inp:
        inp.send_text(keys)
        with create_app_session(input=inp, output=DummyOutput()):
            return ask_questions(settings, defaults, profile)


def test_pressing_enter_everywhere_keeps_defaults(settings, sample_cv_path) -> None:
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    answers = _run(settings, profile, "\r" * 20)
    expected = default_answers(settings, None, profile)
    for key in ("geography", "employment_types", "position_mode", "arrangement", "window", "include_undisclosed",
                "seniority_mode", "sources", "languages", "additional", "include_previous"):
        got, want = answers[key], expected[key]
        if isinstance(want, list):
            got, want = sorted(got), sorted(want)  # checkbox answers come back in display order
        assert got == want, key
    assert answers["min_salary"] is None
    config = build_search_config(answers, settings, profile)
    assert config.publication_window_days == 7 and config.employment_types == ["full_time"]


def test_arrow_keys_change_a_selection(settings, sample_cv_path) -> None:
    profile = CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))
    down = "\x1b[B"
    # Q1 geography: default is "Indonesia + Worldwide" (3rd); one Down -> "Selected countries", then Enter
    # Selected-countries checkbox: Enter keeps pre-checked Indonesia. Remaining questions: Enter.
    answers = _run(settings, profile, down + "\r" + "\r" * 20)
    assert answers["geography"] == "selected"
    assert answers["countries"] == ["Indonesia"]
