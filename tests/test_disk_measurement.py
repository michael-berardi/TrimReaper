"""Synthetic APFS df-versus-statvfs fixtures; no filesystem mutation."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trimreaper import clean, core


class DiskMeasurement(unittest.TestCase):
    def test_apfs_uses_df_used_not_shared_container_total_minus_free(self):
        text = ('Filesystem 1024-blocks Used Available Capacity Mounted on\n'
                '/dev/disk3s5 482797652 422340736 29545784 94% /System/Volumes/Data\n')
        with patch.object(core, 'reap_probe', return_value=(0, text)) as probe, \
                patch.object(core.shutil, 'disk_usage') as statvfs:
            disk = core.reap_disk_state('/System/Volumes/Data')
        statvfs.assert_not_called()
        self.assertEqual(disk['usedBytes'], 422340736 * 1024)
        self.assertEqual(disk['freeBytes'], 29545784 * 1024)
        self.assertEqual(probe.call_args.args[:2], (['df', '-kP', '/System/Volumes/Data'], 5))
        self.assertEqual(probe.call_args.kwargs['env']['LC_ALL'], 'C')
        goal = clean.clean_occupancy(disk, 80)
        self.assertAlmostEqual(goal['usedPercent'], 100 * 422340736 / (422340736 + 29545784))
        self.assertFalse(goal['goalMet'])

    def test_nominal_total_and_printed_percentage_are_not_goal_denominator(self):
        text = 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/synthetic 99999 80 20 99% /Volume With Spaces\n'
        with patch.object(core, 'reap_probe', return_value=(0, text)):
            disk = core.reap_disk_state('/Volume With Spaces')
        self.assertEqual(disk['usedPercent'], 80)
        self.assertTrue(clean.clean_occupancy(disk, 80)['goalMet'])

    def test_failed_or_malformed_df_names_fallback_and_keeps_goal_unknown(self):
        usage = SimpleNamespace(used=90, free=10)
        for result in ((2, 'not available'), (0, 'bad output'),
                       (0, 'header\n/dev/synthetic 200 nope 10 90% /\n'),
                       (0, 'header\n/dev/synthetic 0 0 0 0% /\n')):
            with self.subTest(result=result), patch.object(core, 'reap_probe', return_value=result), \
                    patch.object(core.shutil, 'disk_usage', return_value=usage):
                disk = core.reap_disk_state('/synthetic')
                self.assertIsNone(disk['usedBytes'])
                self.assertIn('df-', disk['measurementError'])
                self.assertIsNone(clean.clean_occupancy(disk, 80)['goalMet'])

    def test_timeout_is_bounded_named_unknown_not_success(self):
        with patch.object(core, 'reap_probe', side_effect=subprocess.TimeoutExpired('df', 5)), \
                patch.object(core.shutil, 'disk_usage', return_value=SimpleNamespace(used=90, free=10)):
            disk = core.reap_disk_state('/synthetic')
        self.assertIn('df-probe:TimeoutExpired', disk['measurementError'])
        self.assertIsNone(clean.clean_occupancy(disk, 80)['remainingBytes'])

    def test_unavailable_volume_returns_unknown(self):
        with patch.object(core, 'reap_probe', return_value=(1, '')), \
                patch.object(core.shutil, 'disk_usage', side_effect=OSError()):
            self.assertIsNone(core.reap_disk_state('/synthetic'))
