"""OpenSubtitles search must make it obvious which film each result is."""
import requests

import sinhalasub


def _item(movie, year, release, downloads=0, file_id=1, ftype="Movie", lang="en"):
    return {
        "attributes": {
            "release": release,
            "download_count": downloads,
            "language": lang,
            "feature_details": {"movie_name": movie, "year": year,
                                "feature_type": ftype},
            "files": [{"file_id": file_id, "file_name": release}],
        }
    }


def _fake(items, recorder=None):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": items}

    def get(url, params=None, headers=None, timeout=None):
        if recorder is not None:
            recorder.update(params or {})
        return R()

    return get


def test_result_label_always_names_the_film(monkeypatch):
    # a release string with no title in it - the old code showed only this
    monkeypatch.setattr(requests, "get",
                        _fake([_item("Inception", 2010, "2160p UHD Blu-ray")]))
    results = sinhalasub.os_search("k", "Inception")
    assert "Inception" in results[0]["label"]
    assert "2010" in results[0]["label"]


def test_label_includes_release_and_downloads(monkeypatch):
    monkeypatch.setattr(requests, "get", _fake(
        [_item("Inception", 2010, "1080p.BluRay.x264", downloads=45231)]))
    label = sinhalasub.os_search("k", "Inception")[0]["label"]
    assert "1080p.BluRay.x264" in label
    assert "45231" in label or "45,231" in label


def test_exact_title_matches_come_first(monkeypatch):
    monkeypatch.setattr(requests, "get", _fake([
        _item("The Ultimate Gift", 2006, "r1", downloads=900, file_id=1),
        _item("The Gift", 2000, "r2", downloads=10, file_id=2),
        _item("The Gifted", 1999, "r3", downloads=800, file_id=3),
    ]))
    results = sinhalasub.os_search("k", "The Gift")
    # the film actually asked for must be first even with fewer downloads
    assert results[0]["movie"] == "The Gift"


def test_more_downloaded_wins_within_the_same_film(monkeypatch):
    monkeypatch.setattr(requests, "get", _fake([
        _item("The Gift", 2000, "low", downloads=5, file_id=1),
        _item("The Gift", 2000, "high", downloads=5000, file_id=2),
    ]))
    results = sinhalasub.os_search("k", "The Gift")
    assert results[0]["release"] == "high"


def test_year_narrows_the_search(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "get",
                        _fake([_item("The Gift", 2000, "r")], recorder=seen))
    sinhalasub.os_search("k", "The Gift", year="2000")
    assert seen.get("year") == "2000"


def test_year_is_omitted_when_blank(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "get",
                        _fake([_item("The Gift", 2000, "r")], recorder=seen))
    sinhalasub.os_search("k", "The Gift", year="")
    assert "year" not in seen


def test_results_expose_fields_the_ui_needs(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        _fake([_item("Inception", 2010, "rel", 7, file_id=99)]))
    r = sinhalasub.os_search("k", "Inception")[0]
    for field in ("file_id", "movie", "year", "release", "downloads", "label"):
        assert field in r
    assert r["file_id"] == 99


def test_entries_without_a_file_id_are_skipped(monkeypatch):
    bad = _item("X", 2000, "r")
    bad["attributes"]["files"] = [{"file_id": None}]
    monkeypatch.setattr(requests, "get", _fake([bad]))
    assert sinhalasub.os_search("k", "X") == []


def test_missing_feature_details_still_produces_a_row(monkeypatch):
    bare = {"attributes": {"release": "just-a-release",
                           "files": [{"file_id": 5}]}}
    monkeypatch.setattr(requests, "get", _fake([bare]))
    results = sinhalasub.os_search("k", "whatever")
    assert results[0]["file_id"] == 5
    assert "just-a-release" in results[0]["label"]
