from __future__ import annotations

from signalpost.urls import normalize_social_url


def test_a_facebook_post_link_is_not_a_profile():
    assert normalize_social_url("https://www.facebook.com/599407360228640_1552122909951569") is None


def test_a_facebook_page_sub_link_reduces_to_the_page():
    got = normalize_social_url("https://www.facebook.com/trondelagbetong/posts/12345")
    assert got == {"platform": "facebook", "url": "https://facebook.com/trondelagbetong"}
    assert normalize_social_url("https://facebook.com/trondelagbetong/photos/a.1/2/")["url"].endswith("/trondelagbetong")
