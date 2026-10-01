import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xlinux.core import device


class TunnelTests(unittest.TestCase):
    def test_resolves_user_path_before_sudo_and_returns_status(self):
        with tempfile.TemporaryDirectory(prefix='xlinux tunnel ') as root:
            root = Path(root)
            (root / 'pymobiledevice3').touch()
            for uid, prefix in [(1000, ['sudo']), (0, [])]:
                with patch.object(device.config, 'pymobiledevice3_python', return_value=root / 'python'), \
                     patch.object(device.os, 'geteuid', return_value=uid), \
                     patch.object(device.subprocess, 'call', return_value=7) as call:
                    self.assertEqual(device.tunnel(), 7)
                    call.assert_called_once_with(prefix + [str(root / 'pymobiledevice3'), 'remote', 'tunneld'])

    def test_missing_tool_does_not_request_sudo(self):
        with patch.object(device.config, 'pymobiledevice3_python', return_value=Path('/missing/xlinux/python')), \
             patch.object(device.subprocess, 'call') as call:
            with self.assertRaisesRegex(SystemExit, 'run `xlinux setup`'):
                device.tunnel()
            call.assert_not_called()
