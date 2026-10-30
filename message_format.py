"""Rich Markdown delivery with bounded chunks and conservative plain fallback."""
import logging
import re
import string

LOG = logging.getLogger('codex_telegram_format')
PLAIN_LIMIT = 3800
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)[\r\n]*$')


def utf16_size(text):
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def plain_chunks(text, limit=PLAIN_LIMIT):
    part, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield ''.join(part)
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        yield ''.join(part)


def fence_after(line, current):
    match = FENCE.fullmatch(line)
    if not match:
        return current
    marker, info = match.groups()
    if current:
        if marker[0] == current[0][0] and len(marker) >= len(current[0]) and not info.strip():
            return None
        return current
    if marker[0] == '`' and '`' in info:
        return None
    language = info.strip().split()[0] if info.strip() else ''
    if not re.fullmatch(r'[A-Za-z0-9_+.-]{0,32}', language):
        language = ''
    return marker, language


def rendered(source, start_fence, end_fence):
    prefix = start_fence[0] + start_fence[1] + '\n' if start_fence else ''
    suffix = ('' if source.endswith('\n') else '\n') + end_fence[0] if end_fence else ''
    return prefix + source + suffix


def message_parts(text, rich=False, limit=PLAIN_LIMIT, max_lines=180):
    """Keep source slices verbatim; fence wrappers exist only in rich_text."""
    lines = text.splitlines(keepends=True)
    if not rich or any((match := FENCE.fullmatch(line)) and len(match[1]) > 32 for line in lines):
        for part in plain_chunks(text, limit):
            yield part, None
        return
    parts, size, line_count = [], 0, 0
    fence = start_fence = None
    for line in lines:
        segments = list(plain_chunks(line, limit))
        for i, segment in enumerate(segments):
            width = utf16_size(segment)
            if parts and (size + width > limit or line_count >= max_lines):
                source = ''.join(parts)
                yield source, rendered(source, start_fence, fence)
                parts, size, line_count, start_fence = [], 0, 0, fence
            parts.append(segment)
            size += width
            line_count += 1
            if i == len(segments) - 1:
                fence = fence_after(line, fence)
    if parts:
        source = ''.join(parts)
        yield source, rendered(source, start_fence, fence)


def formatting_error(error):
    code = getattr(error, 'code', None)
    if code == 404:
        return True  # Older Bot API implementations may not expose sendRichMessage.
    if code != 400:
        return False
    description = str(error).lower()
    return any(word in description for word in (
        "can't parse", 'cannot parse', 'failed to parse', 'invalid markdown', 'invalid html',
        'rich_message', 'rich message is too', 'too many blocks', 'too many columns',
        'unsupported start tag', 'entity offset', 'url host is empty'))


def escape_label(label):
    return ''.join('\\' + char if char in string.punctuation else char for char in label)


async def send_output(telegram, *, chat_id, text, rich_text=None, topic_id=None, label=None):
    label = str(label)[:128] if label else None
    route = {'chat_id': chat_id}
    if topic_id:
        route['message_thread_id'] = topic_id
    plain = (f'〔{label}〕\n' if label else '') + text
    if rich_text is not None and getattr(telegram, 'rich_supported', None) is not False:
        markdown = (f'〔{escape_label(label)}〕\n\n' if label else '') + rich_text
        try:
            result = await telegram.call('sendRichMessage', **route, rich_message={'markdown': markdown})
            telegram.rich_supported = True
            return result
        except Exception as error:
            # Never fall back on unknown delivery, throttling, permissions or a missing topic.
            if not formatting_error(error):
                raise
            if getattr(error, 'code', None) == 404:
                telegram.rich_supported = False
            LOG.warning('Rich Markdown rejected (%s); sending this source chunk as plain text',
                        getattr(error, 'code', None))
    return await telegram.call('sendMessage', **route, text=plain)
