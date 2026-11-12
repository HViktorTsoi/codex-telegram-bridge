import copy
import unittest

from turn_state import TurnState


class TurnStateTests(unittest.TestCase):
    def setUp(self):
        self.state = TurnState()

    def test_exact_started_id_overrides_only_unflushed_interrupted_projection(self):
        original = [{'id': 'work', 'status': 'interrupted', 'startedAt': 100, 'completedAt': None},
                    {'id': 'old', 'status': 'interrupted', 'completedAt': None}]
        saved = copy.deepcopy(original)
        self.state.started({'id': 'work', 'status': 'inProgress', 'startedAt': 100})
        merged = self.state.merge(original, 10)
        self.assertEqual([turn['status'] for turn in merged], ['inProgress', 'interrupted'])
        self.assertNotIn('completedAt', merged[0])
        self.assertEqual(original, saved)

    def test_unrelated_thread_runtime_activity_cannot_reopen_an_old_turn(self):
        history = [{'id': 'old', 'status': 'interrupted', 'completedAt': None}]
        self.assertEqual(self.state.merge(history, 10), history)
        self.state.started({'id': 'new', 'startedAt': 200})
        merged = self.state.merge(history, 10)
        self.assertEqual([(turn['id'], turn['status']) for turn in merged],
                         [('new', 'inProgress'), ('old', 'interrupted')])

    def test_start_ack_is_exact_live_evidence_before_history_flush(self):
        self.state.acknowledge({'id': 'accepted'})
        self.assertEqual(self.state.merge([], 10), [{'id': 'accepted', 'status': 'inProgress'}])
        self.assertEqual(self.state.merge([{'id': 'accepted', 'status': 'interrupted'}], 10)[0]['status'], 'inProgress')

    def test_true_completed_failed_and_timestamped_interrupted_stay_terminal(self):
        for status, ending in [('completed', None), ('failed', None), ('interrupted', 120)]:
            with self.subTest(status=status):
                state = TurnState()
                state.started({'id': 'work'})
                terminal = {'id': 'work', 'status': status, 'completedAt': ending}
                self.assertEqual(state.merge([terminal], 10), [terminal])
                self.assertFalse(state.active)
                self.assertEqual(state.merge([{'id': 'work', 'status': 'inProgress'}], 10), [terminal])

    def test_terminal_event_without_timestamp_wins_and_late_ack_cannot_reopen(self):
        for status in ('completed', 'failed', 'interrupted'):
            with self.subTest(status=status):
                state = TurnState()
                state.started({'id': 'work'})
                state.finished({'id': 'work', 'status': status})
                self.assertFalse(state.active)
                self.assertFalse(state.acknowledge({'id': 'work', 'status': 'inProgress'}))
                self.assertFalse(state.started({'id': 'work'}))
                self.assertEqual(state.merge([{'id': 'work', 'status': 'inProgress'}], 10)[0]['status'], status)

    def test_terminal_start_ack_is_respected_without_a_start_event(self):
        self.state.acknowledge({'id': 'fast', 'status': 'completed', 'completedAt': 101})
        self.assertFalse(self.state.active)
        self.assertEqual(self.state.merge([{'id': 'fast', 'status': 'interrupted'}], 10)[0]['status'], 'completed')

    def test_reset_changes_epoch_and_discards_all_connection_evidence(self):
        self.state.started({'id': 'work'})
        self.state.finished({'id': 'finished', 'status': 'completed'})
        old_epoch = self.state.epoch
        self.state.reset()
        self.assertNotEqual(self.state.epoch, old_epoch)
        self.assertFalse(self.state.active)
        self.assertFalse(self.state.terminal)
        self.assertFalse(self.state.activations)
        history = [{'id': 'work', 'status': 'interrupted'}, {'id': 'finished', 'status': 'inProgress'}]
        self.assertEqual(self.state.merge(history, 10), history)

    def test_invalid_events_never_create_liveness_or_terminal_evidence(self):
        for value in (None, [], {}, {'id': ''}, {'id': 1}):
            self.assertFalse(self.state.started(value))
            self.assertFalse(self.state.finished(value))
            self.assertFalse(self.state.acknowledge(value))
        self.assertFalse(self.state.finished({'id': 'work', 'status': 'inProgress'}))
        self.assertFalse(self.state.active)
        self.assertFalse(self.state.terminal)

    def test_live_and_terminal_evidence_are_bounded_and_limit_is_honored(self):
        for index in range(self.state.LIMIT + 5):
            self.state.started({'id': str(index)})
            self.state.finished({'id': 'done-' + str(index), 'status': 'completed'})
        self.assertEqual(len(self.state.active), self.state.LIMIT)
        self.assertEqual(len(self.state.terminal), self.state.LIMIT)
        self.assertLessEqual(len(self.state.activations), self.state.LIMIT * 2)
        self.assertEqual(len(self.state.merge([], 3)), 3)
        self.assertNotIn('0', self.state.active)
        self.assertNotIn('done-0', self.state.terminal)

    def test_live_evidence_keeps_only_metadata_not_whole_task_contents(self):
        self.state.started({'id': 'work', 'items': [{'text': 'large active history'}]})
        self.assertNotIn('items', self.state.active['work'])
        self.state.finished({'id': 'work', 'status': 'completed', 'items': [{'text': 'large final history'}]})
        self.assertNotIn('items', self.state.terminal['work'])

    def test_older_missing_live_id_cannot_displace_present_current_turn_for_stop(self):
        self.state.started({'id': 'old', 'startedAt': 100})
        self.state.started({'id': 'current', 'startedAt': 200})
        history = [{'id': 'current', 'status': 'inProgress', 'startedAt': 200}]
        self.assertEqual(self.state.merge(history, 1)[0]['id'], 'current')
        self.assertEqual([turn['id'] for turn in self.state.merge(history, 10)], ['current', 'old'])

    def test_newer_missing_live_id_precedes_the_older_present_turn_for_stop(self):
        self.state.started({'id': 'old', 'startedAt': 100})
        self.state.started({'id': 'current', 'startedAt': 200})
        history = [{'id': 'old', 'status': 'interrupted', 'completedAt': None}]
        self.assertEqual(self.state.merge(history, 1)[0]['id'], 'current')
        self.assertEqual([turn['id'] for turn in self.state.merge(history, 10)], ['current', 'old'])

    def test_delayed_sparse_ack_preserves_first_start_time_and_activation_order(self):
        self.state.started({'id': 'old', 'startedAt': 100})
        self.state.started({'id': 'current', 'startedAt': 200})
        self.state.acknowledge({'id': 'old', 'startedAt': None})
        self.state.started({'id': 'old', 'startedAt': 999})
        self.assertEqual(self.state.active['old']['startedAt'], 100)
        self.assertEqual(list(self.state.active), ['old', 'current'])
        history = [{'id': 'current', 'status': 'inProgress', 'startedAt': 200}]
        self.assertEqual(self.state.merge(history, 1)[0]['id'], 'current')

    def test_history_without_live_evidence_preserves_native_order_and_metadata(self):
        history = [{'id': 'newer', 'status': 'completed', 'completedAt': 300},
                   {'id': 'older', 'status': 'interrupted', 'completedAt': None}]
        self.assertEqual(self.state.merge(history, 10), history)
        self.state.started({'id': 'active', 'startedAt': 200})
        history.insert(1, {'id': 'active', 'status': 'inProgress', 'startedAt': 200})
        self.assertEqual([turn['id'] for turn in self.state.merge(history, 10)], ['newer', 'active', 'older'])

    def test_newer_finished_turn_is_a_barrier_against_older_missing_active_turn(self):
        self.state.started({'id': 'old', 'startedAt': 100})
        self.state.started({'id': 'current', 'startedAt': 200})
        terminal = {'id': 'current', 'status': 'completed', 'completedAt': 300}
        self.assertEqual(self.state.merge([terminal], 1), [terminal])
        self.state.acknowledge({'id': 'old'})
        self.assertEqual(self.state.merge([terminal], 1), [terminal])
        self.assertEqual([turn['id'] for turn in self.state.merge([terminal], 10)], ['current', 'old'])
        self.assertEqual(self.state.activations['old'], 1)
        self.assertEqual(self.state.activations['current'], 2)

    def test_late_old_terminal_event_does_not_displace_newer_active_turn(self):
        self.state.started({'id': 'old', 'startedAt': 100})
        self.state.started({'id': 'current', 'startedAt': 200})
        self.state.finished({'id': 'old', 'status': 'interrupted'})
        history = [{'id': 'old', 'status': 'interrupted'},
                   {'id': 'current', 'status': 'inProgress'}]
        self.assertEqual(self.state.merge(history, 1)[0]['id'], 'current')
        self.assertEqual(self.state.activations['old'], 1)
        self.assertEqual(self.state.activations['current'], 2)
