"""Display user echoes as literal quotes without changing their queued text."""
import re
import string

from message_format import message_parts, plain_chunks, utf16_size


ITEM_SEPARATOR = '\n\n────────────\n\n'
PREVIEW_NOTE = '（过程较长，预览仅显示最近部分；完整过程将在完成后发送。）\n\n'
PUNCTUATION = re.compile('[' + re.escape(string.punctuation) + ']')
ECHO_TITLES = ('📩 你在 All 说：', '🖥 你在 Codex 客户端说：')


def is_input_echo(key):
    return key.startswith(('main-guard-input:', 'native-user:'))


def quote_markdown(text):
    """Quote literal text, including Markdown punctuation and HTML entities."""
    escaped = PUNCTUATION.sub(lambda match: '\\' + match[0], text)
    return '> ' + re.sub(r'(\r\n|\r|\n)', lambda match: match[0] + '> ', escaped)


def quote_parts(text, limit=3800):
    """Each returned part retains its raw text and starts its own quote."""
    start = 0
    while start < len(text):
        low, high = start + 1, min(len(text), start + limit)
        end = start
        while low <= high:
            middle = (low + high) // 2
            if utf16_size(quote_markdown(text[start:middle])) <= limit:
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end == start:
            raise ValueError('Quote budget is too small')
        raw = text[start:end]
        yield raw, quote_markdown(raw)
        start = end


def echo_header(text):
    """Separate only the first known bridge title on an already identified echo."""
    for title in ECHO_TITLES:
        for newline in ('\r\n', '\n', '\r'):
            prefix = title + newline
            if text.startswith(prefix):
                return prefix, text[len(prefix):]
    return '', text


def display_header(text):
    return PUNCTUATION.sub(lambda match: '\\' + match[0], text.rstrip('\r\n')) + '\n\n'


def echo_parts(text, limit=3800):
    """Keep a generated title outside quotes, retaining every raw source byte."""
    header, body = echo_header(text)
    if not header:
        yield from quote_parts(body, limit)
        return
    if not body:
        for part in plain_chunks(header, limit):
            yield part, part
        return
    display = display_header(header)
    # Tiny custom budgets may need a separate title packet. Never quote or
    # repeat it merely to make the first body character fit.
    if utf16_size(display) + 4 > limit:
        for part in plain_chunks(header, limit):
            yield part, part
        yield from quote_parts(body, limit)
        return
    start = 0
    while start < len(body):
        low, high, end = start + 1, min(len(body), start + limit), start
        while low <= high:
            middle = (low + high) // 2
            if utf16_size(display + quote_markdown(body[start:middle])) <= limit:
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end == start:
            raise ValueError('Quote budget is too small')
        raw = body[start:end]
        yield header + raw, display + quote_markdown(raw)
        header, display, start = '', '', end


def source_items(rows):
    """Rejoin existing source slices before interpreting their display role."""
    items = []
    for row in rows:
        text = row['text']
        if not text:
            continue
        base = row['key'].rsplit(':', 1)[0]
        if items and items[-1][0] == base:
            key, previous, echo = items[-1]
            items[-1] = key, previous + text, echo
        else:
            items.append((base, text, is_input_echo(row['key'])))
    return items


def has_echo(rows):
    return any(row['text'] and is_input_echo(row['key']) for row in rows)


def aggregate_parts(rows, *, rich, limit=3800, prefix=''):
    """Pack source text and its rich display with independent size budgets."""
    items = source_items(rows)
    raw_body = prefix + ITEM_SEPARATOR.join(text for _, text, _ in items)
    if not rich or not any(echo for _, _, echo in items):
        yield from message_parts(raw_body, rich=rich, limit=limit)
        return
    raw_packet, rich_packet = prefix, prefix
    for item_index, (_, text, echo) in enumerate(items):
        separator = ITEM_SEPARATOR if item_index else ''
        budget = limit - utf16_size(separator) - (utf16_size(prefix) if item_index == 0 else 0)
        if echo:
            parts = echo_parts(text, budget)
        else:
            parts = list(message_parts(text, rich=True, limit=budget))
            if any(utf16_size(formatted or raw) > budget for raw, formatted in parts):
                # Existing fence balancing can add up to 98 marker characters.
                parts = message_parts(text, rich=True, limit=max(1, budget - 100))
        for index, (raw, formatted) in enumerate(parts):
            formatted = formatted if formatted is not None else raw
            prefix = separator if index == 0 else ''
            # Continuations have independent quote/fence wrappers. Never join
            # those wrappers in the middle of a logical source line.
            if raw_packet and (index > 0 or utf16_size(raw_packet + prefix + raw) > limit
                               or utf16_size(rich_packet + prefix + formatted) > limit):
                yield raw_packet, rich_packet
                raw_packet, rich_packet = '', ''
            raw_packet += prefix + raw
            rich_packet += prefix + formatted
    if raw_packet:
        yield raw_packet, rich_packet


def preview_pair(rows, limit):
    """Return matching raw/rich tails, trimming source text before quoting."""
    items = source_items(rows)
    segments = []
    for index, (_, text, echo) in enumerate(items):
        if index:
            segments.append((ITEM_SEPARATOR, False))
        if echo:
            header, body = echo_header(text)
            if header:
                segments.append((header, 'header'))
            if body:
                segments.append((body, 'quote'))
        else:
            segments.append((text, False))

    def tail(length):
        result = []
        for text, echo in reversed(segments):
            if length <= 0:
                break
            part = text[-length:]
            result.append((part, echo))
            length -= len(part)
        result.reverse()
        return (''.join(text for text, _ in result),
                ''.join(quote_markdown(text) if echo == 'quote'
                        else display_header(text) if echo == 'header' else text
                        for text, echo in result))

    total = sum(len(text) for text, _ in segments)
    raw, formatted = tail(total)
    if max(utf16_size(raw), utf16_size(formatted)) <= limit:
        return raw, formatted
    low, high, best = 0, min(total, limit), ('', '')
    while low <= high:
        middle = (low + high) // 2
        raw, formatted = tail(middle)
        if max(utf16_size(PREVIEW_NOTE + raw), utf16_size(PREVIEW_NOTE + formatted)) <= limit:
            best, low = (raw, formatted), middle + 1
        else:
            high = middle - 1
    return PREVIEW_NOTE + best[0], PREVIEW_NOTE + best[1]
