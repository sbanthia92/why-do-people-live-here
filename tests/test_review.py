"""Tests for rejecting previewed places from their GitHub issues."""

import json

from pipeline.review import marker, rejected_urls


def test_marker_round_trips_through_an_issue_body():
    url = "https://en.wikipedia.org/wiki/Alpha_Island"
    body = f"# Alpha Island — single post\n\n> text\n\n{marker(url)}\n"
    assert rejected_urls(json.dumps([{"body": body}])) == [url]


def test_urls_are_collected_across_issues_and_deduplicated():
    issues = [
        {"body": f"a\n{marker('https://en.wikipedia.org/wiki/B')}"},
        {"body": f"b\n{marker('https://en.wikipedia.org/wiki/A')}"},
        {"body": f"again\n{marker('https://en.wikipedia.org/wiki/A')}"},
    ]
    assert rejected_urls(json.dumps(issues)) == [
        "https://en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
    ]


def test_issues_without_a_marker_or_body_are_ignored():
    issues = [{"body": "written by hand"}, {"body": None}, {}]
    assert rejected_urls(json.dumps(issues)) == []


def test_empty_input_means_no_rejections():
    assert rejected_urls("") == []
    assert rejected_urls("[]") == []
