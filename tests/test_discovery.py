"""Unit tests for project-name parsing and producer helpers."""

from types import SimpleNamespace

from app.constants import find_project_for_task, get_relevant_names
from app.services.discovery import normalize_project, parse_project_name


def test_parse_joint_project_name():
    parsed = parse_project_name("| Acme Roofing Co. | // Joint // SEO & PPC")
    assert parsed.client_name == "Acme Roofing Co."
    assert parsed.project_type == "Joint"
    assert parsed.service_type == "SEO & PPC"


def test_parse_internal_project_name_with_extra_spacing():
    parsed = parse_project_name("|  Northwind Builders  |  //  Internal  //  Local SEO")
    assert parsed.client_name == "Northwind Builders"
    assert parsed.project_type == "Internal"
    assert parsed.service_type == "Local SEO"


def test_unparseable_project_name_returns_none():
    assert parse_project_name("Website redesign 2026") is None
    assert parse_project_name("| Acme Roofing Co. | // Shared // SEO") is None


def test_normalize_project_fills_defaults_without_overwriting():
    project = normalize_project({"id": 12345678, "producers": ["Sam T."]})
    assert project["producers"] == ["Sam T."]
    assert project["poll_interval_minutes"] is None
    assert project["last_polled_at"] is None


def test_get_relevant_names_merges_and_dedupes():
    names = get_relevant_names({"producers": [" Jordan K. ", "", "Jordan K.", "Sam T."]})
    assert names[1:] == ["Jordan K.", "Sam T."]
    assert len(names) == len(set(names))


def test_get_relevant_names_without_project_is_core_team():
    from app.constants import CORE_TEAM
    assert get_relevant_names(None) == CORE_TEAM


def test_find_project_for_task_matches_string_and_int_ids():
    account = SimpleNamespace(projects=[{"id": 12345678}, {"id": "23456789"}])
    assert find_project_for_task(account, "12345678") == {"id": 12345678}
    assert find_project_for_task(account, 23456789) == {"id": "23456789"}
    assert find_project_for_task(account, "99999999") is None
    assert find_project_for_task(None, "12345678") is None
