import json
from dataclasses import asdict

from parsehub.types import ImageParseResult, RichTextParseResult
from parsehub.types.document import Block, Document, TextRun, document_from_dict, html_blocks, paragraph, plain
from parsehub.types.serialize import result_from_cache_dict, result_to_cache_dict


def test_plain_document_keeps_platform_syntax_literal():
    source = '    || ** \u3000<a href="/literal">\n> {{media:1}} &lt;'
    assert plain(source) == Document((Block('paragraph', (TextRun(source),)),))
    assert plain('').blocks == ()


def test_source_html_keeps_links_structural_and_decodes_entities_once():
    heading, body, code = html_blocks(
        '<h3><em>中文</em></h3><p> &amp;lt; <a href="/a"><b>**</b></a><br>||</p>'
        '<pre><code class="language-python"> x &lt; y\n</code></pre>',
        base_url='https://site.example/post',
    )
    assert heading == Block('heading', (TextRun('中文', styles=('italic',)),), level=3)
    assert body.runs == (TextRun(' &lt; '), TextRun('**', 'https://site.example/a', ('bold',)), TextRun('\n||'))
    assert code == Block('code', (TextRun(' x < y\n'),), language='python')


def test_nested_lists_tables_quotes_and_media():
    quote, details, table = html_blocks(
        '<blockquote><ol start="4"><li><input type="checkbox" checked>'
        'first<ul><li>child</li></ul></li></ol></blockquote>'
        '<details><summary>Open</summary><img data-src="/image.png"><img src="/emoji.png"></details>'
        '<table><thead><tr><th>Left</th><th>Right</th></tr></thead>'
        '<tbody><tr><td>a</td><td><strong>b</strong></td></tr></tbody></table>',
        base_url='https://site.example/thread', media={'https://site.example/image.png': 3},
    )
    lst, = quote.children
    assert lst.ordered and lst.start == 4
    assert lst.items == ((paragraph('☑ first'), Block('list', items=((paragraph('child'),),))),)
    assert details.children == (Block('media', index=3, url='https://site.example/image.png'),)
    assert table.rows[1][1] == (TextRun('b', styles=('bold',)),)
    document = Document((quote, details, table), topic=(TextRun('Topic', 'https://site.example/topic'),))
    assert document_from_dict(json.loads(json.dumps(asdict(document)))) == document


def test_disallowed_links_and_hidden_inert_tags_have_no_effect():
    assert html_blocks('<p><a href="javascript:void(0)">safe label</a><script>hidden</script></p>') == (
        paragraph('safe label'),
    )


def test_result_cache_reconstructs_document_without_changing_public_json():
    document = Document((paragraph((TextRun(' 🐱 ', 'https://site.example/source'),)), Block('poll', index=2)))
    for cls, kwargs in ((ImageParseResult, {'photo': []}), (RichTextParseResult, {})):
        result = cls(document=document, **kwargs)
        assert 'document' not in result.to_dict()
        assert result_from_cache_dict(json.loads(json.dumps(result_to_cache_dict(result)))).document == document
        assert result_from_cache_dict(result_to_cache_dict(cls(**kwargs))).document is None
