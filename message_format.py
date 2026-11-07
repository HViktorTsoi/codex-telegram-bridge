"""Rich Markdown delivery with bounded chunks and conservative plain fallback."""
import logging
import re

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


def code_text(text):
    """Display a destination literally, even when its filename contains backticks."""
    marker = '`' * (max((len(m[0]) for m in re.finditer(r'`+', text)), default=0) + 1)
    return marker + ' ' + text.replace('\n', ' ').replace('\r', ' ') + ' ' + marker


def closing_delimiter(text, start, opening, closing):
    depth, pos = 1, start + 1
    while pos < len(text):
        if text[pos] == '\\':
            pos += 2
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if not depth:
                return pos
        pos += 1
    return None


def display_destination(destination):
    destination = destination.strip()
    if destination.startswith('<') and '>' in destination:
        destination = destination[1:destination.index('>')]
    else:
        # An optional Markdown title is not part of the file path.
        destination = re.sub(r'''\s+(?:"[^"\n]*"|'[^'\n]*')\s*$''', '', destination)
    return re.sub(r'\\([\\`*{}\[\]()#+.!_<> -])', r'\1', destination)


def normalize_local_links(markdown, rewrite=None):
    """Local files cannot be fetched by Telegram; retain them as literal text.

    Operate only on the display copy at delivery, preserving queued source and
    existing chunk boundaries. Code examples and public/Telegram links stay intact.
    The default renderer never reads or uploads a file. An optional callback can
    replace explicit local links before chunking, keeping code examples intact.
    """
    output, fence = [], None
    for line in markdown.splitlines(keepends=True):
        next_fence = fence_after(line, fence)
        if fence or next_fence or FENCE.fullmatch(line):
            output.append(line)
            fence = next_fence
            continue
        pos = 0
        while pos < len(line):
            if line[pos] == '\\' and pos + 1 < len(line):
                output.append(line[pos:pos + 2])
                pos += 2
                continue
            if line[pos] == '`':
                marker = re.match(r'`+', line[pos:])[0]
                match = re.search(r'(?<!`)' + re.escape(marker) + r'(?!`)', line[pos + len(marker):])
                end = pos + len(marker) + match.end() if match else pos + len(marker)
                output.append(line[pos:end])
                pos = end
                continue
            image = line.startswith('![', pos)
            start = pos + 1 if image else pos
            if line[start:start + 1] == '[':
                label_end = closing_delimiter(line, start, '[', ']')
                if label_end is not None and line[label_end + 1:label_end + 2] == '(':
                    end = closing_delimiter(line, label_end + 1, '(', ')')
                    if end is not None:
                        destination = display_destination(line[label_end + 2:end])
                        if destination and not re.match(r'(?i)^(?:https?://|tg://|mailto:|tel:|#)', destination):
                            label = line[start + 1:label_end] or ('图片' if image else '文件')
                            if rewrite is not None:
                                replacement = rewrite(label, destination, image)
                                output.append(line[pos:end + 1] if replacement is None else replacement)
                                pos = end + 1
                                continue
                            kind = '本机图片，尚未上传' if image else '本机文件'
                            output.append(label + '（' + kind + '：' + code_text(destination) + '）')
                        else:
                            output.append(line[pos:end + 1])
                        pos = end + 1
                        continue
            output.append(line[pos])
            pos += 1
    return ''.join(output)


def formatting_reason(error):
    description = str(error).lower()
    if 'url' in description:
        return 'invalid_url'
    if getattr(error, 'code', None) == 404:
        return 'unsupported_method'
    return 'invalid_format'


async def send_output(telegram, *, chat_id, text, rich_text=None, topic_id=None, label=None, media_json=None,
                      disable_notification=None):
    """Send the body only; legacy label metadata is accepted without rendering it."""
    route = {'chat_id': chat_id}
    if disable_notification is not None:
        route['disable_notification'] = disable_notification
    if topic_id:
        route['message_thread_id'] = topic_id
    if media_json is not None:
        from outgoing_images import send_image
        return await send_image(telegram, media_json, route)
    if rich_text is not None and getattr(telegram, 'rich_supported', None) is not False:
        markdown = normalize_local_links(rich_text)
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
            LOG.warning('Rich Markdown rejected (%s, %s); sending this source chunk as plain text',
                        getattr(error, 'code', None), formatting_reason(error))
    return await telegram.call('sendMessage', **route, text=text)
