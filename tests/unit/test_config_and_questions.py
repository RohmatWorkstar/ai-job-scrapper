from __future__ import annotations

from pathlib import Path

import pytest

from src.cli.questions import build_search_config, default_answers, parse_list, recommended_seniority
from src.core.configuration import Credentials, load_settings
from src.core.exceptions import ConfigurationError
from src.cv.analyzer import CVAnalyzer
from src.cv.parser import parse_cv
from src.storage.history import PreferenceStore


def test_settings_load(settings) -> None:
    assert settings.timezone == "Asia/Jakarta"
    assert settings.get("matching.weights.position") == 25
    assert sum(settings.get("matching.weights").values()) == 100
    assert "Indonesia" in settings.country_table()
    assert settings.get("discovery.connectors")


def test_invalid_config_rejected(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("matching:\n  weights: {position: -5}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path, load_env=False)
    (tmp_path / "settings.yaml").write_text("performance:\n  max_concurrency: 0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path, load_env=False)
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path / "missing", load_env=False)


def test_credentials_never_printed() -> None:
    c = Credentials(gemini_api_key="AIzaSecretValue123", search_api_key="BSAsecret999", search_provider="brave")
    text = repr(c) + str(c)
    assert "AIzaSecretValue123" not in text and "BSAsecret999" not in text
    assert "<set>" in text
    assert c.has_ai and c.has_search


def _profile(settings, sample_cv_path):
    return CVAnalyzer(settings).analyze(parse_cv(sample_cv_path))


def test_enter_defaults_build_valid_config(settings, sample_cv_path) -> None:
    profile = _profile(settings, sample_cv_path)
    answers = default_answers(settings, None, profile)
    config = build_search_config(answers, settings, profile)
    assert config.countries == ["Indonesia"] and config.include_worldwide
    assert config.employment_types == ["full_time"]
    assert config.publication_window_days == 7
    assert config.include_undisclosed_salary is True
    assert set(config.languages) == {"en", "id"}
    assert config.target_positions and config.target_positions[0] == "Fullstack Developer"
    assert len(config.source_categories) == 7


def test_saved_preferences_override_defaults(settings, sample_cv_path, tmp_path: Path) -> None:
    profile = _profile(settings, sample_cv_path)
    store = PreferenceStore(tmp_path / "p.json")
    store.save({"answers": {"geography": "selected", "countries": ["Singapore", "Malaysia"], "window": "30",
                            "arrangement": "remote_hybrid", "employment_types": ["all"]}})
    answers = default_answers(settings, store.load()["answers"], profile)
    config = build_search_config(answers, settings, profile)
    assert config.countries == ["Singapore", "Malaysia"] and not config.include_worldwide
    assert config.publication_window_days == 30
    assert config.work_arrangements == ["remote", "hybrid"]
    assert "internship" in config.employment_types


def test_positions_modes(settings, sample_cv_path) -> None:
    profile = _profile(settings, sample_cv_path)
    a = default_answers(settings, None, profile)
    a.update(position_mode="custom", custom_positions=["API Developer", " Node.js Developer "])
    assert build_search_config(a, settings, profile).target_positions == ["API Developer", " Node.js Developer "]
    a.update(position_mode="current_title")
    assert build_search_config(a, settings, profile).target_positions == ["Fullstack Developer"]
    a.update(window="all", min_salary="15000000", salary_currency="IDR", seniority_mode="senior")
    c = build_search_config(a, settings, profile)
    assert c.publication_window_days is None and c.min_salary == 15_000_000
    assert c.seniority_levels == ["senior"] and c.seniority_mode == "custom"


def test_recommended_seniority_is_adjacent(settings, sample_cv_path) -> None:
    profile = _profile(settings, sample_cv_path)
    profile.seniority_estimate = "mid"
    assert recommended_seniority(profile) == ["junior", "mid", "senior"]


def test_parse_list() -> None:
    assert parse_list("a, b;c\n d") == ["a", "b", "c", "d"]
