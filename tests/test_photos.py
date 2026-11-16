"""Tests for pipeline.photos.photos_for_candidate: the photos a post carries."""

from pathlib import Path

import httpx

from pipeline import photos


def _record(name, source, usable=True):
    return {
        "name": name, "source": source, "usable": usable, "reason": None, "license": "CC0",
        "description": "A street", "page_url": f"https://commons.example/File:{name}",
        "thumb_url": "https://upload.example/t.jpg", "mime": "image/jpeg", "image": None,
        "shows": None, "reference": None, "width": 1600, "height": 900,
    }  # fmt: skip


def _world(monkeypatch, found, rejected=()):
    asked = []

    async def fake_photos_for(client, title):
        asked.append(title)
        return list(found)

    async def fake_check(client, place, records, compare=False):
        for record in records:
            if record["name"] in rejected:
                record.update(usable=False, reason="AI check (a recognisable person): students")
            elif record["usable"]:
                record.update(image=b"jpegbytes", shows="a harbour street")

    monkeypatch.setattr(photos, "photos_for", fake_photos_for)
    monkeypatch.setattr(photos, "check_with_ai", fake_check)
    return asked


async def test_accepted_photos_are_saved_category_first_up_to_the_limit(monkeypatch, tmp_path):
    found = [_record("a.jpg", "article"), _record("b.jpg", "category"), _record("c.jpg", "category")]
    asked = _world(monkeypatch, found)

    used = await photos.photos_for_candidate(
        "Shell, Ecuador", "https://en.wikipedia.org/wiki/Shell%2C_Ecuador", tmp_path, limit=2
    )

    assert asked == ["Shell, Ecuador"]  # the article's title, decoded
    assert [u["page_url"].rsplit(":", 1)[-1] for u in used] == ["b.jpg", "c.jpg"]  # limit=2
    assert Path(used[0]["path"]) == tmp_path / "shell--ecuador-photo-1.jpg"
    assert Path(used[0]["path"]).read_bytes() == b"jpegbytes"
    assert used[0]["alt_text"] == "Photo of Shell, Ecuador: a harbour street"
    assert used[0]["license"] == "CC0"


async def test_rejected_photos_are_never_used(monkeypatch, tmp_path):
    found = [_record("students.jpg", "category"), _record("cc-by.jpg", "category", usable=False)]
    _world(monkeypatch, found, rejected={"students.jpg"})

    assert await photos.photos_for_candidate("Bhilai", "https://en.wikipedia.org/wiki/Bhilai", tmp_path) == []
    assert list(tmp_path.iterdir()) == []


async def test_a_failed_lookup_means_no_photos_and_never_raises(monkeypatch, tmp_path):
    async def broken(client, title):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(photos, "photos_for", broken)

    assert await photos.photos_for_candidate("Alpha", "https://en.wikipedia.org/wiki/Alpha", tmp_path) == []


def test_photos_are_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv("POST_IMAGES", raising=False)
    assert photos.enabled()
    monkeypatch.setenv("POST_IMAGES", "")  # an unset repository variable
    assert photos.enabled()
    monkeypatch.setenv("POST_IMAGES", "false")
    assert not photos.enabled()
