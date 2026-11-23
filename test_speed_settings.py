"""Speed labels and choices distinguish native state from missing metadata."""
import unittest

from speed_settings import current_speed, speed_choices, speed_label, speed_matches


class SpeedSettingsTests(unittest.TestCase):
    def test_missing_or_invalid_native_state_is_never_assumed_to_be_standard(self):
        for value in [{}, None, {'serviceTier': {}}, {'serviceTier': ''}]:
            with self.subTest(value=value):
                self.assertIsNone(current_speed(value))
                self.assertIn('未知', speed_label(value))
                self.assertFalse(speed_matches(value, 'default'))

    def test_explicit_null_and_standard_aliases_match_standard_but_fast_aliases_match_fast(self):
        for value in [None, 'default', 'standard']:
            with self.subTest(value=value):
                native = {'serviceTier': value}
                self.assertEqual(current_speed(native), 'default')
                self.assertEqual(speed_label(native), 'Standard')
                self.assertTrue(speed_matches(native, 'default'))
                self.assertFalse(speed_matches(native, 'fast'))
        for value in ['fast', 'priority']:
            native = {'serviceTier': value}
            self.assertEqual(current_speed(native), 'fast')
            self.assertEqual(speed_label(native), 'Fast')
            self.assertTrue(speed_matches(native, 'fast'))
            self.assertTrue(speed_matches(native, 'priority'))
            self.assertFalse(speed_matches(native, 'default'))

    def test_other_native_tiers_remain_visible_without_appearing_as_standard_or_fast(self):
        native = {'serviceTier': 'ultrafast'}
        self.assertEqual(current_speed(native), 'ultrafast')
        self.assertIn('ultrafast', speed_label(native))
        self.assertFalse(speed_matches(native, 'default'))
        self.assertFalse(speed_matches(native, 'fast'))

    def test_fast_uses_the_modern_catalog_id_instead_of_the_deprecated_alias(self):
        choices = speed_choices({'serviceTiers': [{'id': 'priority', 'name': 'Fast'}],
                                 'additionalSpeedTiers': ['fast']})
        self.assertEqual([item['id'] for item in choices], ['default', 'priority'])

    def test_legacy_catalog_is_used_only_when_modern_metadata_is_absent(self):
        self.assertEqual([x['id'] for x in speed_choices({'additionalSpeedTiers': ['fast']})],
                         ['default', 'fast'])
        for tiers in [[], None, 'fast', [{'id': 'ultrafast', 'name': 'Fast'}]]:
            with self.subTest(tiers=tiers):
                choices = speed_choices({'serviceTiers': tiers, 'additionalSpeedTiers': ['fast']})
                self.assertEqual([item['id'] for item in choices], ['default'])


if __name__ == '__main__':
    unittest.main()
