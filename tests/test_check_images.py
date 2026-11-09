"""Tests for scripts/check_images.py (no network: canned Commons answers)."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_images.py"
_spec = importlib.util.spec_from_file_location("check_images", _SCRIPT)
check_images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_images)


def _page(name, license_code="cc0", attribution="false", mime="image/jpeg", repo="shared", **meta):
    def field(value):
        return {"value": value}

    extmetadata = {
        "License": field(license_code),
        "LicenseShortName": field(license_code.upper()),
        "AttributionRequired": field(attribution),
        "ImageDescription": field("<b>A  shop</b> on the main street"),
        **{k: field(v) for k, v in meta.items()},
    }
    return {
        "title": f"File:{name}",
        "imagerepository": repo,
        "imageinfo": [
            {
                "mime": mime,
                "width": 1600,
                "height": 900,
                "url": "https://upload.example/full.jpg",
                "thumburl": "https://upload.example/thumb.jpg",
                "descriptionurl": f"https://commons.example/File:{name}",
                "extmetadata": extmetadata,
            }
        ],
    }


def test_public_domain_photo_is_usable_and_described_in_plain_text():
    record = check_images.photo_record(_page("Main street.jpg"), "article")
    assert record["usable"] and record["reason"] is None
    assert record["license"] == "CC0"
    assert record["description"] == "A shop on the main street"


def test_attribution_licence_is_rejected_for_the_same_reason_the_pipeline_gives():
    page = _page("Church.jpg", license_code="cc-by-sa-4.0", attribution="true")
    record = check_images.photo_record(page, "article")
    assert not record["usable"]
    assert "not CC0 or public domain" in record["reason"]


def test_rights_warning_and_local_uploads_and_svgs_are_rejected():
    assert "rights warning" in check_images.photo_record(
        _page("Person.jpg", Restrictions="personality"), "article"
    )["reason"]
    assert "not hosted on Wikimedia Commons" in check_images.photo_record(
        _page("Local.jpg", repo="local"), "article"
    )["reason"]
    assert "type image/svg+xml" in check_images.photo_record(
        _page("Church.svg", mime="image/svg+xml"), "article"
    )["reason"]


def test_flags_maps_and_logos_are_not_counted_as_photos():
    for name in ("Flag of Alaska.png", "Locator map.png", "Town logo.png", "Coat of arms.png"):
        assert check_images.photo_record(_page(name), "article") is None


def test_report_summarises_each_place_and_why_photos_were_rejected():
    good = check_images.photo_record(_page("Main street.jpg"), "category")
    bad = check_images.photo_record(
        _page("Church.jpg", license_code="cc-by-sa-4.0", attribution="true"), "article"
    )
    report = check_images.render({"Alpha Island": [good, bad], "Empty Town": []})

    assert "| Alpha Island | 2 | 1 | 1 |" in report
    assert "| Empty Town | 0 | 0 | 0 |" in report
    assert "- **Main street.jpg** (1600×900, CC0, from the category)" in report
    assert "Rejected 1: licence CC-BY-SA-4.0 is not CC0 or public domain ×1." in report
    assert "No photos found in the article or the Commons category." in report
