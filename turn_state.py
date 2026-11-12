"""Connection-local execution evidence, independent of persisted turn history."""
from collections import OrderedDict
import math
import secrets


TERMINAL = frozenset(('completed', 'failed', 'interrupted'))


class TurnState:
    """Prefer exact live turn IDs over a not-yet-flushed history projection.

    Thread-wide ``active`` state alone cannot identify a turn. Only a started
    event or the corresponding start acknowledgement establishes live evidence.
    No evidence survives a connection change.
    """

    LIMIT = 128

    def __init__(self):
        self.reset()

    def reset(self):
        self.epoch = secrets.token_hex(12)
        self.active = OrderedDict()
        self.terminal = OrderedDict()
        self.activations = OrderedDict()
        self.sequence = 0

    @staticmethod
    def _valid(turn):
        return isinstance(turn, dict) and isinstance(turn.get('id'), str) and bool(turn['id'])

    @staticmethod
    def _ended(turn):
        value = turn.get('completedAt')
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    def _bound(self, entries):
        while len(entries) > self.LIMIT:
            entries.popitem(last=False)

    def _remember(self, identity):
        if identity not in self.activations:
            self.sequence += 1
            self.activations[identity] = self.sequence
            while len(self.activations) > self.LIMIT * 2:
                self.activations.popitem(last=False)

    @staticmethod
    def _metadata(turn):
        return {key: turn[key] for key in ('id', 'status', 'startedAt', 'completedAt') if key in turn}

    def started(self, turn):
        if not self._valid(turn) or turn['id'] in self.terminal:
            return False  # A delayed start/ACK must not reopen a finished ID.
        if turn['id'] not in self.active:
            self._remember(turn['id'])
        current = dict(self.active.get(turn['id'], {}))
        for key, value in self._metadata(turn).items():
            if value is not None and (key != 'startedAt' or current.get(key) is None):
                current[key] = value
        current['status'] = 'inProgress'
        current.pop('completedAt', None)
        self.active[turn['id']] = current
        # First activation determines order. A sparse or delayed start ACK for
        # an older turn must not promote it ahead of a newer active turn.
        self._bound(self.active)
        return True

    def finished(self, turn):
        if not self._valid(turn) or turn.get('status') not in TERMINAL:
            return False
        self._remember(turn['id'])
        self.active.pop(turn['id'], None)
        self.terminal[turn['id']] = self._metadata(turn)
        self.terminal.move_to_end(turn['id'])
        self._bound(self.terminal)
        return True

    def acknowledge(self, turn):
        if not self._valid(turn) or turn['id'] in self.terminal:
            return False
        return self.finished(turn) if turn.get('status') in TERMINAL else self.started(turn)

    def merge(self, turns, limit):
        result = []
        for turn in turns:
            current = dict(turn)
            identity = current.get('id')
            if identity in self.terminal:
                current.update(self.terminal[identity])
            elif identity in self.active:
                if (current.get('status') in TERMINAL
                        and (current['status'] != 'interrupted' or self._ended(current))):
                    # Only the unflushed interrupted projection is ambiguous;
                    # completed/failed and timestamped endings remain terminal.
                    self.finished(current)
                else:
                    current['status'] = 'inProgress'
                    current.pop('completedAt', None)
            result.append(current)
        # Rank present terminal IDs and live IDs by their shared *first*
        # activation. B's terminal history must block a missing older A, and a
        # late terminal event for A must never displace a newer active B.
        present = {turn['id']: turn for turn in result if turn.get('id') in self.activations}
        known = dict(present)
        for identity, turn in self.active.items():
            known.setdefault(identity, dict(turn))
        ordered = sorted(known.values(), key=lambda turn: self.activations.get(turn['id'], -1), reverse=True)
        if present:
            first = next(index for index, turn in enumerate(result) if turn.get('id') in present)
            return (result[:first] + ordered + [turn for turn in result[first:] if turn.get('id') not in present])[:limit]
        return (ordered + result)[:limit]
