"""Render preview paragraphs as small, secondary-color Telegram footers."""
from html import escape


def _image_text(renderer, tokens, index, options, env):
    """Keep image references readable without emitting preview media blocks."""
    token = tokens[index]
    label = renderer.renderInlineAsText(token.children or [], options, env) or '图片'
    source = token.attrGet('src') or ''
    text = label if not source or source == label else label + ' (' + source + ')'
    return escape(text, quote=False)


def render_footer_markdown(markdown):
    """Return safe Rich HTML while keeping structured blocks at normal size.

    Parser imports and instances are local so unused preview styles have no
    dependency on this renderer and parsing never shares mutable token state.
    """
    from markdown_it import MarkdownIt

    parser = MarkdownIt('commonmark', {
        'html': False, 'linkify': False, 'typographer': False, 'breaks': True,
    }).enable(['table', 'strikethrough'])
    parser.add_render_rule('image', _image_text)
    env = {}
    tokens = parser.parse(markdown, env)
    for token in tokens:
        if token.type in ('paragraph_open', 'paragraph_close') and token.level == 0:
            token.tag = 'footer'
        elif token.type in ('thead_open', 'thead_close', 'tbody_open', 'tbody_close'):
            # Telegram supports table/tr/th/td, without these HTML wrappers.
            token.hidden = True
        elif token.type in ('th_open', 'td_open'):
            style = token.attrs.pop('style', '')
            alignment = style.removeprefix('text-align:').strip(' ;')
            if alignment in ('left', 'center', 'right'):
                token.attrSet('align', alignment)
    return parser.renderer.render(tokens, parser.options, env)
