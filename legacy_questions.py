"""Read exact async question sources from local legacy Codex rollouts."""
import json
import os
from pathlib import Path
import stat


def _rollout_roots():
    homes = {Path.home() / '.codex'}
    configured = os.environ.get('CODEX_HOME')
    if configured and Path(configured).is_absolute():
        homes.add(Path(configured))
    return tuple(home / directory for home in homes for directory in ('sessions', 'archived_sessions'))


def read_question_source(path, thread_id, turn_id, item_id, *, roots=None):
    """Return only a verified question's title/options, never rollout text.

    Legacy full-turn history replaces tool call IDs with ``item-N``. The local
    rollout retains the original ID; its session and turn must also match.
    """
    if not isinstance(path, str) or not Path(path).is_absolute():
        return None
    if any(not isinstance(value, str) or not value for value in (thread_id, turn_id, item_id)):
        return None
    source_path = Path(path).resolve(strict=True)
    allowed = _rollout_roots() if roots is None else roots
    if (source_path.suffix != '.jsonl'
            or not any(source_path.is_relative_to(Path(root).resolve()) for root in allowed)
            or not stat.S_ISREG(source_path.stat().st_mode)):
        return None
    from choice_questions import _questions

    session, current_turn, source = False, None, None
    encoded_id = item_id.encode()
    with source_path.open('rb') as rollout:
        for line in rollout:
            if b'"session_meta"' not in line and b'"turn_context"' not in line and encoded_id not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
                continue
            payload = record['payload']
            if record.get('type') == 'session_meta':
                if payload.get('id') != thread_id:
                    return None
                session = True
            elif record.get('type') == 'turn_context':
                current_turn = payload.get('turn_id')
            elif (record.get('type') == 'response_item' and payload.get('type') == 'function_call'
                  and payload.get('call_id') == item_id):
                if not session or current_turn != turn_id or payload.get('name') != 'request_user_input_async':
                    return None
                try:
                    arguments = json.loads(payload.get('arguments', ''))
                except (ValueError, TypeError):
                    return None
                questions = _questions(arguments.get('questions'), 'async') if isinstance(arguments, dict) else None
                if not questions:
                    return None
                candidate = {'id': item_id, 'type': 'agentMessage', 'delivery': 'async',
                             'questions': [{'title': question['title'], 'options': question['options']}
                                           for question in questions]}
                if source is not None and source != candidate:
                    return None
                source = candidate
    return source if session else None
