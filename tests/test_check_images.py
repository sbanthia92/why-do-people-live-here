"""Tests for scripts/check_images.py (no network: canned Commons answers)."""

import importlib.util
from pathlib import Path

import httpx

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


def test_flags_maps_and_logos_are_not_counted_as_photos_in_any_language():
    for name in (
        "Flag of Alaska.png",
        "Locator map.png",
        "Town logo.png",
        "Coat of arms.png",
        "Yuzhnoukrayinsk prapor.png",  # Ukrainian for flag
        "Yuzhnoukrainsk gerb.PNG",  # Ukrainian for coat of arms
    ):
        assert check_images.photo_record(_page(name), "article") is None


def test_a_map_is_caught_by_its_description_even_if_its_name_is_plain():
    page = _page("Ontong java 1934.jpg", ImageDescription="Map of Ontong Java Atoll")
    assert check_images.photo_record(page, "article") is None


def test_a_word_that_merely_contains_map_is_not_mistaken_for_one():
    assert check_images.photo_record(_page("Maple Street shops.jpg"), "article") is not None


def test_small_and_thin_files_are_rejected_with_the_reason():
    small = _page("Hospital.jpg")
    small["imageinfo"][0].update(width=111, height=83)
    strip = _page("Panorama.jpg")
    strip["imageinfo"][0].update(width=2166, height=562)

    assert "too small" in check_images.photo_record(small, "category")["reason"]
    assert "wrong shape" in check_images.photo_record(strip, "article")["reason"]


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
    assert "No photos found in the article or the exact Commons category." in report


async def test_only_the_exact_commons_category_is_used_never_a_shorter_name(monkeypatch):
    asked: list[str] = []

    async def fake_pages(client, api, params):
        if "gcmtitle" in params:
            asked.append(params["gcmtitle"])
        return []

    monkeypatch.setattr(check_images, "_pages", fake_pages)

    await check_images.photos_for(None, "Livingston, Guatemala")

    assert asked == ["Category:Livingston, Guatemala"]  # not "Category:Livingston"


class _Verdict:
    def __init__(self, accepted, what="a street", reason="matches", is_photo=True, place="yes"):
        self.accepted, self.what_it_shows, self.reason = accepted, what, reason
        self.is_photo, self.place_match = is_photo, place


def _record(name, source="category"):
    return check_images.photo_record(_page(name), source)


class _FakeClient:
    """Serves a tiny 'image' for any thumbnail address."""

    async def get(self, url, follow_redirects=True):
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=b"imagebytes", headers={"content-type": "image/jpeg"}, request=request)


async def test_the_ai_check_keeps_accepted_photos_and_rejects_the_rest_with_its_reason(monkeypatch):
    good, wrong = _record("Main street.jpg"), _record("St Elmo.jpg", "article")
    verdicts = {
        "Main street.jpg": _Verdict(True, "a street of shops"),
        "St Elmo.jpg": _Verdict(False, "ghost town in Colorado", "names St. Elmo", place="no"),
    }

    async def fake_judge(place, image, media_type, name, description, source):
        assert (image, media_type) == (b"imagebytes", "image/jpeg")
        return verdicts[name]

    monkeypatch.setattr(check_images, "judge_photo", fake_judge)

    await check_images.check_with_ai(_FakeClient(), "Afton, Nevada", [good, wrong])

    assert good["usable"] and good["shows"] == "a street of shops"
    assert not wrong["usable"]
    assert wrong["reason"] == "AI check (place: no): names St. Elmo"


async def test_a_photo_the_ai_could_not_check_is_not_accepted(monkeypatch):
    photo = _record("Main street.jpg")

    async def failing_judge(*args):
        raise check_images.llm.LLMRefusalError("declined")

    monkeypatch.setattr(check_images, "judge_photo", failing_judge)

    await check_images.check_with_ai(_FakeClient(), "Alpha", [photo])

    assert not photo["usable"] and photo["reason"] == "not checked (LLMRefusalError)"


async def test_no_more_than_the_limit_goes_to_the_ai(monkeypatch):
    photos = [_record(f"Street {i}.jpg") for i in range(check_images._MAX_JUDGED + 3)]
    judged: list[str] = []

    async def fake_judge(place, image, media_type, name, description, source):
        judged.append(name)
        return _Verdict(True)

    monkeypatch.setattr(check_images, "judge_photo", fake_judge)

    await check_images.check_with_ai(_FakeClient(), "Alpha", photos)

    assert len(judged) == check_images._MAX_JUDGED
    assert sum(p["usable"] for p in photos) == check_images._MAX_JUDGED
    assert photos[-1]["reason"] == "not checked (limit per place)"
