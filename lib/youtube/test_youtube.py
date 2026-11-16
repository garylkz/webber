"""Offline tests for youtube_channel: the search page parser and the clear-match rule.

The fixture is a real channel search for "alex meyers", cut down to its channel
results. The rest of the tests use small made-up result lists.
"""

from pathlib import Path

import youtube_channel as y

PAGE = (Path(__file__).parent / "fixtures" / "youtube_search_alex_meyers.html").read_text(encoding="utf-8")


def ch(name, channel, subscribers):
    return {"name": name, "channel": channel, "subscribers": subscribers}


def test_the_search_page_is_parsed_in_order():
    channels = y.parse_channels(PAGE)
    assert channels[0] == ch("Alex Meyers", "@AlexMeyersVids", channels[0]["subscribers"])
    assert channels[0]["subscribers"].endswith("subscribers")
    assert all(set(c) == {"name", "channel", "subscribers"} for c in channels)


def test_a_page_with_no_data_has_no_channels():
    assert y.parse_channels("<html></html>") == []


def test_the_real_search_finds_alex_meyers():
    result = y.choose("alex meyers", y.parse_channels(PAGE))
    assert result["status"] == "found"
    assert result["channel"] == "@AlexMeyersVids"
    assert result["url"] == "https://www.youtube.com/@AlexMeyersVids"


def test_subscriber_counts():
    assert y.subscriber_count("3.93M subscribers") == 3_930_000
    assert y.subscriber_count("260K subscribers") == 260_000
    assert y.subscriber_count("12 subscribers") == 12
    assert y.subscriber_count("") == 0


def test_a_close_rival_makes_it_ambiguous():
    channels = [ch("Alex", "@alexwelford", "260K subscribers"), ch("alex", "@Alexalex", "1.18M subscribers")]
    result = y.choose("alex", channels)
    assert result["status"] == "ambiguous"
    assert [c["channel"] for c in result["candidates"]] == ["@Alexalex", "@alexwelford"]
    assert [c["url"] for c in result["candidates"]] == ["https://www.youtube.com/@Alexalex",
                                                       "https://www.youtube.com/@alexwelford"]


def test_a_five_times_lead_is_clear():
    channels = [ch("Alex", "@alex", "1.3M subscribers"), ch("Alex Two", "@alex2", "260K subscribers")]
    assert y.choose("alex", channels)["channel"] == "@alex"


def test_case_spaces_and_punctuation_are_ignored():
    assert y.choose("MKBHD", [ch("Marques Brownlee", "@mkbhd", "20M subscribers")])["status"] == "found"
    assert y.choose("alex-meyers!", [ch("Alex Meyers", "@x", "1M subscribers")])["status"] == "found"


def test_no_close_result_is_none():
    assert y.choose("zqxwvplorkt", [ch("Alex Meyers", "@AlexMeyersVids", "3.9M subscribers")]) is None


def test_traditional_and_simplified_match_through_the_map():
    channels = [ch("科學漫聊", "@kxml", "500K subscribers")]
    assert y.choose("科学漫聊", channels) is None
    simplified = {"科學漫聊": "科学漫聊"}
    assert y.choose("科学漫聊", channels, simplified)["status"] == "found"


def test_a_channel_without_a_handle_links_by_id():
    assert y.channel_url("UCabc") == "https://www.youtube.com/channel/UCabc"
