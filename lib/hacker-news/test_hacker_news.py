"""Offline tests for hacker_news_comments: ids, comment text, the limits and moved threads.

The fixture is the Algolia record of Hacker News item 1, a real thread that no
longer changes. The rest use small made-up trees.
"""

import json
from pathlib import Path

import hacker_news_comments as h

ITEM_1 = json.loads((Path(__file__).parent / "fixtures" / "hacker_news_item_1.json").read_text(encoding="utf-8"))


def node(text, *children):
    return {"text": text, "children": list(children)}


def test_an_id_from_a_url_or_a_bare_number():
    assert h.resolve_id("https://news.ycombinator.com/item?id=42") == "42"
    assert h.resolve_id("https://x.example/?id=1&next=item?id=42") == "42"  # the last id= wins
    assert h.resolve_id("42") == "42"
    assert h.resolve_id("abc") is None


def test_comment_html_becomes_plain_text():
    assert h.clean('a<p>b <a href="https://x.example/p" rel="nofollow">https://x.example/...</a>') == "a\nb https://x.example/p"
    assert h.clean("x &amp; y &lt;3 &#x27;q&#39; &gt;") == "x & y <3 'q' >"
    assert h.clean("a&nbsp;b") == "a b"
    assert h.clean("keep &unknown; as is") == "keep &unknown; as is"
    assert h.clean(None) == ""


def test_a_long_comment_is_cut():
    text = "word " * 200
    cut = h.trim_body(text.strip())
    assert cut.endswith(" [...]") and len(cut) <= h.MAX_CHARS + len(" [...]")
    assert h.trim_body("short") == "short"


def test_item_1_builds_the_thread():
    out = h.build(ITEM_1, "1")
    assert list(out)[:10] == ["id", "title", "points", "url", "comments_url", "moved_from",
                              "top_level_comments", "shown", "max_depth", "comments"]
    assert out["id"] == 1 and out["moved_from"] is None and out["max_depth"] == h.MAX_DEPTH
    assert out["comments_url"] == "https://news.ycombinator.com/item?id=1"
    assert out["top_level_comments"] == len(ITEM_1["children"])
    assert out["shown"] > 0 and "note" not in out


def test_empty_comments_are_skipped_and_counted_apart():
    item = {"title": "T", "points": 1, "url": None, "children": [node(""), node("hi"), node(None)]}
    out = h.build(item, "7")
    assert out["top_level_comments"] == 3 and out["shown"] == 1
    assert out["comments"] == [{"text": "hi", "replies": []}]


def test_no_comments_says_so():
    assert h.build({"title": "T", "children": []}, "7")["note"] == "No comments yet."


def test_the_tree_stops_at_the_depth_and_the_count():
    deep = node("1", node("2", node("3", node("4", node("5")))))
    comments, shown = h.comment_tree({"children": [deep]})
    assert shown == 4
    level4 = comments[0]["replies"][0]["replies"][0]["replies"][0]
    assert level4 == {"text": "4", "replies": []}
    many = {"children": [node(str(i)) for i in range(h.MAX_COMMENTS + 5)]}
    comments, shown = h.comment_tree(many)
    assert shown == h.MAX_COMMENTS and len(comments) == h.MAX_COMMENTS


def test_a_moved_thread_is_found():
    moved = {"children": [node('Comments moved to <a href="https://news.ycombinator.com/item?id=99">here</a>.')]}
    assert h.moved_target(moved) == "99"
    assert h.moved_target({"children": [node("Comments moved to item?id=99"), node("another")]}) is None
    assert h.moved_target(ITEM_1) is None
