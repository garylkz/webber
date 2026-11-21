"""Small synthetic fixtures, never complete copyrighted chapters or live calls."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import novelight_novels as n

FORM = '''<form class="filter-container"><label for="id_genres_1"><input name="genres" value="7"/>Fantasy</label>
<label><input name="tags" id="id_tags_421" value="400">Limited Lifespan</label>
<label><input name="tags" value="2">Magic</label></form>'''
CARD = '<a class="item" href="/book/a-destiny-that-must-die"><div class="title">A Destiny That Must Die</div></a>'
CAT = FORM + '<div class="manga-grid-list">' + CARD + '</div><a href="?page=2">2</a>'
CHAPTER = '''<a href="/book/chapter/264593" class="chapter"><div class="title">4 chapter</div>
<div class="chapter-info"><span class="author"><i></i>Reader</span><span class="date">01.01.2026</span></div></a>'''
DETAIL = '''<h1>A Destiny That Must Die</h1><div id="information">
<section class="text-info"><p>A short synopsis.</p><p>Another paragraph.<br/>Next line.</p></section>
<section class="tags"><a href="/catalog/?tags=400">Limited Lifespan</a></section>
<section class="text-info"><div class="translators"><a class="translator" href="/team/sakuraze">Sakuraze</a></div></section>
</div><div class="second-information"><div class="mini-info"><a class="item"><div class="sub-header">Status</div><div class="info">Completed</div></a>
<a href="/catalog/?genres=7">Fantasy</a></div></div>
<div id="all-chapters-list">''' + CHAPTER + '''</div>
<select id="select-pagination-chapter"><option value="1">1-30</option><option value="2">31-60</option></select>
<div class="manga-list swp-single">''' + CARD + '''</div><script>const BOOK_ID = '1217';</script>'''
WRAPPER = '''<title>A Destiny That Must Die - 4 chapter</title><header class="header-chapter">
<a class="header-title" href="/book/a-destiny-that-must-die">A Destiny That Must Die</a>
<a href="/book/chapter/264594">Next<i class="fa fa-angle-right"></i></a>
<a href="/book/chapter/264592"><i class="fa fa-angle-left"></i>Previous</a></header><div class="chapter-text__place">Loading...</div>'''
PROSE = {'class': 'zakqmj', 'content': '''<div class="chapter-text zakqmj"><div>First line &amp; thought.</div>
<div class="advertisment-inner">BUY NOW<script>bad()</script></div><p>Second line.<br>Third line.</p>
<!-- not prose --><nav>Next</nav><style>.x{}</style><script>alert(1)</script>
<div hidden>Secret</div><div style="display: none">Hidden</div><div aria-hidden="true">Hidden</div>
<div class="payment">Pay to continue</div><aside>Widget</aside></div>'''}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('Offline test attempted network access')
    monkeypatch.setattr(requests.sessions.Session, 'request', fail)


@pytest.fixture
def cache_dir():
    # Keep test files inside the repo even when pytest's default temp root is not.
    root = n.REPO_ROOT / 'out' / 'pytest-novelight'
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix='cache-', dir=root))
    yield path
    shutil.rmtree(path)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, 'Unexpected extra request'
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, text = item if isinstance(item, tuple) else (200, item)
        return type('Response', (), {'status_code': status, 'text': text})()


def client(cache_dir, *responses):
    session = Session(responses)
    return n.Client(cache_dir, session=session), session


def test_catalogue_scope_and_taxonomies():
    parsed = n.parse_catalogue(CAT)
    assert len(parsed['novels']) == 1
    assert parsed['coverage'] == {'current_page': 1, 'next_page': 2, 'pages': [1, 2], 'returned': 1,
                                  'catalogue_exhaustive': False,
                                  'scope': 'One catalogue page; source page size is 30. Page links are not a total count.'}
    assert n.parse_taxonomies(CAT)['tags'][0] == {'id': 400, 'name': 'Limited Lifespan'}
    assert len(n.parse_catalogue(CAT + '<div class="recommendations">' + CARD + '</div>')['novels']) == 1


def test_empty_and_malformed():
    for content in ['', '<p>No results found</p>']:
        assert n.parse_catalogue(FORM + '<div class="manga-grid-list">' + content + '</div>')['novels'] == []
    # A search with no matches omits the results grid but keeps the filter form.
    assert n.parse_catalogue(FORM)['novels'] == []
    for html in ['<html>Login</html>', FORM + '<div class="manga-grid-list">Unexpected markup</div>',
                 CAT.replace('class="title"', 'class="oops"'), CAT.replace('/book/a-destiny-that-must-die', 'https://evil.test/book/bad')]:
        with pytest.raises(n.NovelightError):
            n.parse_catalogue(html)
    with pytest.raises(n.NovelightError):
        n.parse_taxonomies('<form class="filter-container"></form>')


def test_detail_and_reading_selectors():
    data = n.parse_detail(DETAIL)
    assert data['description'] == 'A short synopsis.\nAnother paragraph.\nNext line.'
    assert data['book_id'] == 1217
    assert data['metadata'] == {'Status': 'Completed'}
    assert data['translators'] == [{'name': 'Sakuraze', 'url': n.BASE_URL + '/team/sakuraze'}]
    assert data['genres'] == [{'id': 7, 'name': 'Fantasy'}]
    assert len(data['chapters']) == 1
    assert data['chapters'][0]['author'] == 'Reader'
    assert n.parse_reading(PROSE)['text'] == 'First line & thought.\nSecond line.\nThird line.'
    wrapper = n.parse_wrapper(WRAPPER)
    assert wrapper['novel_url'].endswith('/book/a-destiny-that-must-die')
    assert wrapper['previous_url'].endswith('/264592')
    assert wrapper['next_url'].endswith('/264594')


@pytest.mark.parametrize('data', [{}, {'content': ''}, {'content': '<div>Loading...</div>'},
    {'content': '<div class="chapter-text">Loading...</div>'},
    {'content': '<div class="chapter-text">Please log in to read this chapter</div>'},
    {'content': '<div class="chapter-text">Purchase to read this chapter</div>'},
    {'content': '<div class="chapter-text"><div class="advertisment">BUY</div></div>'}])
def test_unreadable(data):
    with pytest.raises(n.NovelightError):
        n.parse_reading(data)


def test_void_self_closing_and_malformed_closing():
    text = n.DOM('<div><p>A<br/>B<img src="x"><input><hr>C</wrong></p><p>D</p></div>').root.text()
    assert text == 'A\nB\nC\nD'
    assert n.DOM('<div>A<!-- comment --><script>not prose</script><span/>B</div>').root.text() == 'AB'


def test_shared_catalogue_cache_and_filters(cache_dir):
    c, s = client(cache_dir, CAT, CAT)
    first = n.search_novels(client=c)
    assert first['provenance'][0]['source'] == 'network'
    assert n.get_taxonomies(client=c)['provenance'][0]['source'] == 'cache'
    filtered = n.search_novels(genres=['fantasy'], tags=['limited lifespan', 400], page=2, client=c)
    assert filtered['query']['genres'] == [7]
    assert filtered['query']['tags'] == [400]
    assert s.calls[1][1]['params']['page'] == 2
    n.search_novels(genres=[7], tags=[400], page=2, client=c)
    assert len(s.calls) == 2
    assert all(not call[1]['allow_redirects'] for call in s.calls)
    assert s.calls[0][1]['headers'] == n.HEADERS
    with pytest.raises(n.NovelightError, match='Unknown'):
        n.search_novels(tags=['not real'], client=c)
    with pytest.raises(n.NovelightError, match='ambiguous'):
        n._resolve(['Magic'], [{'id': 1, 'name': 'Magic'}, {'id': 2, 'name': 'MAGIC'}], 'tags')


@pytest.mark.parametrize('mutate', ['corrupt', 'future', 'expired', 'identity', 'invalid', 'naive'])
def test_cache_ignored(cache_dir, mutate):
    c, s = client(cache_dir, CAT, CAT)
    n.search_novels(client=c)
    path = next(cache_dir.glob('*.json'))
    row = json.loads(path.read_text())
    if mutate == 'corrupt':
        path.write_text('{bad json')
    else:
        if mutate in {'future', 'expired'}:
            row['fetched_at'] = (datetime.now(timezone.utc) + timedelta(days=2 if mutate == 'future' else -2)).isoformat()
        elif mutate == 'identity':
            row['identity']['mode'] = 'json'
        elif mutate == 'invalid':
            row['raw'] = '<html>unavailable</html>'
        else:
            row['fetched_at'] = '2026-01-01T00:00:00'
        path.write_text(json.dumps(row))
    assert n.search_novels(client=c)['provenance'][0]['source'] == 'network'
    assert len(s.calls) == 2


def test_refresh_no_stale_fallback(cache_dir):
    c, s = client(cache_dir, CAT, (403, 'Forbidden'))
    n.search_novels(client=c)
    with pytest.raises(n.NovelightError, match='403'):
        n.search_novels(refresh=True, client=c)
    assert len(s.calls) == 2
    assert n.search_novels(client=c)['provenance'][0]['source'] == 'cache'


@pytest.mark.parametrize('response', [(404, 'Not found'), (302, 'Redirect'), requests.Timeout('timeout'), 'malformed'])
def test_failures_not_cached(cache_dir, response):
    c, s = client(cache_dir, response)
    with pytest.raises(n.NovelightError):
        n.search_novels(client=c)
    assert not list(cache_dir.glob('*.json'))


def test_cache_writes_warn_preserve_success(cache_dir, monkeypatch, capsys):
    c, _ = client(cache_dir, CAT)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(n.os, 'replace', fail)
    assert n.search_novels(client=c)['novels']
    assert 'Warning:' in capsys.readouterr().err
    assert list(cache_dir.iterdir()) == []


def test_independent_wrapper_ajax_cache_and_headers(cache_dir):
    c, s = client(cache_dir, WRAPPER, json.dumps(PROSE))
    first = n.get_chapter(264593, client=c)
    assert first['text_available'] and 'BUY' not in first['text']
    assert first['url'] == n.BASE_URL + '/book/chapter/264593'
    headers = s.calls[1][1]['headers']
    assert headers['X-Requested-With'] == 'XMLHttpRequest'
    assert headers['Accept'] == 'application/json, text/javascript, */*; q=0.01'
    assert headers['Referer'] == first['url']
    assert [p['source'] for p in n.get_chapter(264593, client=c)['provenance']] == ['cache', 'cache']
    assert len(list(cache_dir.glob('*.json'))) == 2
    assert len(s.calls) == 2


def test_detail_pagination_and_cache(cache_dir):
    c, s = client(cache_dir, DETAIL, json.dumps({'html': CHAPTER.replace('264593', '264594')}))
    first = n.get_novel('a-destiny-that-must-die', client=c)
    assert first['chapter_coverage']['next_page'] == 2
    second = n.get_novel('a-destiny-that-must-die', chapter_page=2, client=c)
    assert second['chapters'][0]['id'] == 264594
    assert second['chapter_coverage']['current_page'] == 2
    assert s.calls[1][1]['params'] == {'book_id': 1217, 'page': 2}
    assert s.calls[1][1]['headers']['Referer'].endswith('/book/a-destiny-that-must-die')
    n.get_novel('a-destiny-that-must-die', chapter_page=2, client=c)
    with pytest.raises(n.NovelightError, match='out of range'):
        n.get_novel('a-destiny-that-must-die', chapter_page=3, client=c)
    assert len(s.calls) == 2


def test_detail_chapter_limit(cache_dir):
    many = DETAIL.replace(CHAPTER, ''.join(CHAPTER.replace('264593', str(264593 + i)) for i in range(3)))
    c, s = client(cache_dir, many)
    shown = n.get_novel('a-destiny-that-must-die', client=c, chapter_limit=2)
    assert [ch['id'] for ch in shown['chapters']] == [264593, 264594]
    cov = shown['chapter_coverage']
    assert (cov['on_page'], cov['returned'], cov['truncated']) == (3, 2, True)
    full = n.get_novel('a-destiny-that-must-die', client=c)
    assert len(full['chapters']) == 3 and full['chapter_coverage']['truncated'] is False
    assert len(s.calls) == 1
    for bad in (0, -1, True, '2'):
        with pytest.raises(n.NovelightError, match='chapter_limit'):
            n.get_novel('a-destiny-that-must-die', client=c, chapter_limit=bad)


@pytest.mark.parametrize('invoke', [lambda c: n.search_novels(page=0, client=c), lambda c: n.search_novels(page=True, client=c),
    lambda c: n.search_novels(search='\n', client=c), lambda c: n.search_novels(tags='Magic', client=c),
    lambda c: n.search_novels(genres=[True], client=c), lambda c: n.get_novel('../bad', client=c),
    lambda c: n.get_novel('safe', chapter_page=0, client=c), lambda c: n.get_chapter(-1, client=c),
    lambda c: n.get_chapter(True, client=c)])
def test_validation_before_network(cache_dir, invoke):
    c, s = client(cache_dir)
    with pytest.raises(n.NovelightError):
        invoke(c)
    assert s.calls == []


@pytest.mark.parametrize('args', [['--detail', 'safe', '--search', 'x'], ['--taxonomies', '--page', '1'],
    ['--chapter', '1', '--tag', 'Magic'], ['--chapter-page', '1'], ['--chapter-limit', '1'], ['--detail', 'x', '--chapter', '1'],
    ['--per-page', '2']])
def test_cli_incompatible_arguments(args, capsys):
    with pytest.raises(SystemExit) as exc:
        n.main(args)
    assert exc.value.code != 0
    output = capsys.readouterr()
    assert output.out == '' and output.err


def test_cli_json_errors_and_defaults(monkeypatch, capsys):
    calls = []
    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {'source': 'novelight'}
    monkeypatch.setattr(n, 'search_novels', fake)
    assert n.main([]) == 0
    assert calls[0][0] == (None, [], [], 1)
    assert json.loads(capsys.readouterr().out) == {'source': 'novelight'}
    assert n.main(['--chapter', '0']) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'positive integer' in captured.err


def test_safe_paths(cache_dir):
    c, s = client(cache_dir)
    for path in ['https://evil.test', '/book/../bad', '/team/sakuraze', '/book/ajax/write']:
        with pytest.raises(n.NovelightError):
            c.fetch(path, validate=lambda x: x)
    for url in ['http://novelight.net/book/x', 'https://evil.test/book/x', '//evil.test/book/x']:
        with pytest.raises(n.NovelightError):
            n._link(url, r'/book/[a-z]+')
    assert s.calls == []


def test_unavailable_ajax_preserves_wrapper_only(cache_dir):
    c, s = client(cache_dir, WRAPPER, (403, 'Forbidden'), json.dumps(PROSE))
    with pytest.raises(n.NovelightError, match='403'):
        n.get_chapter(264593, client=c)
    assert len(list(cache_dir.glob('*.json'))) == 1
    result = n.get_chapter(264593, client=c)
    assert [p['source'] for p in result['provenance']] == ['cache', 'network']
    assert len(s.calls) == 3


def test_expired_never_falls_back(cache_dir):
    c, s = client(cache_dir, CAT, requests.ConnectionError('offline'))
    n.search_novels(client=c)
    path = next(cache_dir.glob('*.json'))
    row = json.loads(path.read_text())
    row['fetched_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(row))
    with pytest.raises(n.NovelightError, match='offline'):
        n.search_novels(client=c)
    assert len(s.calls) == 2


@pytest.mark.parametrize('data', [{}, {'html': 1}, {'html': '<p>No chapters</p>'},
    {'html': CHAPTER.replace('/book/chapter/264593', 'https://translator.test/chapter/264593')},
    {'html': CHAPTER.replace('class="title"', 'class="wrong"')}])
def test_malformed_chapter_pagination(data):
    with pytest.raises(n.NovelightError):
        n.parse_chapter_page(data)


@pytest.mark.parametrize('filters', [None, True, 1, 'Magic', {'Magic'}, {'name': 'Magic'}])
def test_invalid_filter_containers_rejected_before_network(cache_dir, filters):
    c, s = client(cache_dir)
    for kind in ('genres', 'tags'):
        with pytest.raises(n.NovelightError, match='list or tuple'):
            n.search_novels(client=c, **{kind: filters})
    assert s.calls == []


def test_empty_detail_and_bad_refresh_rejected(cache_dir, capsys):
    assert n.main(['--detail=']) == 1
    assert 'slug' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        n.main(['--detail=', '--search=magic'])
    c, s = client(cache_dir)
    with pytest.raises(n.NovelightError, match='refresh'):
        n.search_novels(refresh='false', client=c)
    assert s.calls == []


def test_cache_identity_includes_header_mode(cache_dir, monkeypatch):
    c, s = client(cache_dir, CAT, CAT)
    n.search_novels(client=c)
    monkeypatch.setitem(n.HEADERS, 'Accept-Language', 'en-GB')
    n.search_novels(client=c)
    assert len(s.calls) == 2
    assert len(list(cache_dir.glob('*.json'))) == 2
