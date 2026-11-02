"""Bounded snapshots of explicit Telegram replies, separate from message content."""
import json

MAX_TEXT = 12000
MAX_QUOTE = 4000
MEDIA = ('photo', 'document', 'voice', 'audio', 'video', 'video_note', 'animation', 'sticker')


def rich_text(value):
    budget = [2048]
    limited = [False]

    def parts(values, limit=256):
        if len(values) > limit:
            limited[0] = True
        return values[:limit]

    def walk(node, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 16:
            limited[0] = True
            return ''
        if isinstance(node, str):
            return node[:MAX_TEXT + 1]
        if isinstance(node, list):
            return ''.join(walk(part, depth + 1) for part in parts(node))[:MAX_TEXT + 1]
        if not isinstance(node, dict):
            return ''
        if isinstance(node.get('blocks'), list):
            return '\n'.join(walk(part, depth + 1) for part in parts(node['blocks']))[:MAX_TEXT + 1]
        cells = node.get('cells', node.get('rows'))
        if isinstance(cells, list):
            return '\n'.join(' | '.join(walk(cell, depth + 1) for cell in parts(row, 64))
                             for row in parts(cells, 128) if isinstance(row, list))[:MAX_TEXT + 1]
        if isinstance(node.get('items'), list):
            return '\n'.join(walk(part, depth + 1) for part in parts(node['items']))[:MAX_TEXT + 1]
        if 'text' in node:
            return walk(node['text'], depth + 1)
        if 'caption' in node:
            return walk(node['caption'], depth + 1)
        if 'expression' in node:
            return walk(node['expression'], depth + 1)
        if node.get('type') in ('photo', 'video', 'audio', 'animation', 'document', 'voice_note'):
            return '[' + node['type'] + '附件]'
        return ''

    body = walk(value)
    return body[:MAX_TEXT], limited[0] or len(body) > MAX_TEXT


def snapshot(message):
    parent = message.get('reply_to_message')
    external = message.get('external_reply')
    quote = message.get('quote')
    if not any(isinstance(value, dict) and value for value in (parent, external, quote)):
        return None
    # Some clients include the topic's creation message as an implicit root reply.
    if isinstance(parent, dict) and 'forum_topic_created' in parent and not quote and not external:
        return None
    context = {}
    if isinstance(parent, dict) and parent:
        context['source'] = 'reply_to_message'
        source = parent
    elif isinstance(external, dict) and external:
        context['source'] = 'external_reply'
        source = external
    else:
        context['source'] = 'quote'
        source = {}
    chat_id = source.get('chat', {}).get('id') if isinstance(source.get('chat'), dict) else None
    if chat_id is None and context['source'] == 'reply_to_message':
        chat_id = message.get('chat', {}).get('id')
    for key, value in (('chat_id', chat_id), ('reply_to_message_id', source.get('message_id')),
                       ('reply_to_thread_id', source.get('message_thread_id'))):
        if isinstance(value, int) and not isinstance(value, bool):
            context[key] = value
    original = source.get('text') or source.get('caption')
    truncated = False
    if isinstance(original, str):
        truncated = len(original) > MAX_TEXT
        body = original[:MAX_TEXT]
    elif isinstance(source.get('rich_message'), dict):
        body, truncated = rich_text(source['rich_message'])
    else:
        body = ''
    if body:
        context['text'] = body
    if truncated:
        context['text_truncated'] = True
    if isinstance(quote, dict) and isinstance(quote.get('text'), str):
        context['quote'] = quote['text'][:MAX_QUOTE]
        if len(quote['text']) > MAX_QUOTE:
            context['quote_truncated'] = True
        if isinstance(quote.get('position'), int):
            context['quote_position_utf16'] = quote['position']
        if isinstance(quote.get('is_manual'), bool):
            context['quote_is_manual'] = quote['is_manual']
    media = []
    for kind in MEDIA:
        if source.get(kind):
            item = {'kind': kind}
            if isinstance(source[kind], dict) and isinstance(source[kind].get('file_name'), str):
                item['file_name'] = source[kind]['file_name'][:256]
            media.append(item)
    if media:
        context['attachments'] = media
    if not body:
        context['content_unavailable'] = True
    return context


def serialized(message, context=None):
    context = snapshot(message) if context is None else context
    return json.dumps(context, ensure_ascii=False) if context else None


def render(context):
    def quoted(text):
        return '\n'.join('> ' + line for line in text.split('\n'))

    sections = ['［引用内容］']
    body, quote = context.get('text'), context.get('quote')
    if quote:
        label = '用户选中的片段（优先关注）：'
        sections.append(label + '\n' + quoted(quote))
        if context.get('quote_truncated'):
            sections.append('（选中片段过长，以上内容已截断。）')
    if body and body != quote:
        label = '被回复消息的正文（供理解上下文）：\n' if quote else ''
        sections.append(label + quoted(body))
    if body and context.get('text_truncated'):
        sections.append('（被回复消息正文过长，以上内容已截断。）')
    if not body:
        sections.append('未收到被回复消息的正文。' if not quote else '仅收到上述选中片段，未收到原消息正文。')
    names = {'photo': '图片', 'document': '文件', 'voice': '语音', 'audio': '音频', 'video': '视频',
             'video_note': '视频消息', 'animation': '动画', 'sticker': '贴纸'}
    media = [names.get(item['kind'], item['kind']) + ('：' + item['file_name'] if item.get('file_name') else '')
             for item in context.get('attachments', [])]
    if media:
        sections.append('被引用的附件（仅提供说明，未重新下载）：\n' + quoted('\n'.join(media)))
    return '\n\n'.join(sections)


def inputs(rows):
    result, seen = [], set()
    for row in rows:
        raw = row['reply_json'] if 'reply_json' in row.keys() else None
        if raw and raw not in seen:
            seen.add(raw)
            result.append({'type': 'text', 'text': render(json.loads(raw))})
    return result
