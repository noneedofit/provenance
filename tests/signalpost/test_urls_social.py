from __future__ import annotations

from signalpost.urls import normalize_social_url


def test_a_facebook_post_link_is_not_a_profile():
    assert normalize_social_url("https://www.facebook.com/599407360228640_1552122909951569") is None


def test_a_facebook_page_sub_link_reduces_to_the_page():
    got = normalize_social_url("https://www.facebook.com/trondelagbetong/posts/12345")
    assert got == {"platform": "facebook", "url": "https://facebook.com/trondelagbetong"}
    assert normalize_social_url("https://facebook.com/trondelagbetong/photos/a.1/2/")["url"].endswith("/trondelagbetong")


def test_facebook_share_photo_and_watch_links_are_not_profiles():
    for url in ("https://www.facebook.com/share/1AbCdEf/", "https://www.facebook.com/photo.php?fbid=1",
                "https://www.facebook.com/watch/?v=1", "https://www.facebook.com/permalink.php?story_fbid=1",
                "https://www.facebook.com/hashtag/norge"):
        assert normalize_social_url(url) is None, url
    assert normalize_social_url("https://www.facebook.com/egonor")["url"] == "https://facebook.com/egonor"


def test_a_website_builders_own_accounts_are_not_the_companys_profiles():
    for url in ("https://www.facebook.com/WixStudio", "https://www.instagram.com/wix/",
                "https://www.linkedin.com/company/wix-com", "https://www.youtube.com/@squarespace"):
        assert normalize_social_url(url) is None, url
