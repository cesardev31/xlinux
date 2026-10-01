import base64
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xlinux.adapters.flutter import build
from xlinux.core import app, deps, extensions
from xlinux.core.xcode import project as xp


class FlutterTests(unittest.TestCase):
    def test_multiple_defines_assemble(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'pubspec.yaml').write_text('name: test\n')
            defines = root / 'qa.json'
            defines.write_text(json.dumps({'API': 'a,b', 'ENV': 'qa'}))
            p = build.Project(root, False, 'qa')
            p.dart_defines = build.read_dart_defines(['EXTRA=yes'], [defines])
            with patch.object(build, 'run') as run, patch.object(build.shutil, 'copytree'):
                build.assemble(p, root / 'Frameworks')
            args = run.call_args.args[0]
            encoded = next(a.split('=', 1)[1] for a in args if a.startswith('--DartDefines='))
            self.assertEqual([base64.b64decode(d).decode() for d in encoded.split(',')],
                             ['API=a,b', 'ENV=qa', 'EXTRA=yes'])
            self.assertIn('-dConfiguration=Release-qa', args)
            self.assertIn('-dFlavor=qa', args)
            self.assertFalse(any(a.startswith('-dDartDefines=') for a in args))
            p = build.Project(root, True)
            with patch.object(build, 'run') as run, patch.object(build.shutil, 'copytree'):
                build.assemble(p, root / 'Frameworks')
            self.assertIn('-dConfiguration=Debug', run.call_args.args[0])
            self.assertFalse(any(a.startswith('-dFlavor=') for a in run.call_args.args[0]))

    def test_flavor_settings_and_extensions(self):
        ruby = deps.cocoapods_env()
        if not Path(ruby['XLINUX_RUBY']).exists():
            self.skipTest('requires the installed CocoaPods Ruby/xcodeproj gem')
        fixture = Path(__file__).parent / 'fixtures/Runner.xcodeproj'
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for mode, suffix in [('Release', ''), ('Debug-qa', '.qa'), ('Release-qa', '.qa')]:
                description = xp.dump(fixture, mode, ruby)
                description['project_dir'] = str(root)
                project = xp.XcodeProject(description, root / 'build', mode)
                settings = project.settings(project.targets['Runner'])
                self.assertEqual(settings.get('PRODUCT_BUNDLE_IDENTIFIER'), 'com.example.roda' + suffix)
                info = app.expand({'name': '$(APP_DISPLAY_NAME)', 'id': '${PRODUCT_BUNDLE_IDENTIFIER}'}, settings)
                self.assertEqual(info['name'], 'Roda QA' if suffix else 'Roda')
                widget = root / 'Widget'
                widget.mkdir(exist_ok=True)
                with (widget / 'Info.plist').open('wb') as f:
                    plistlib.dump({'NSExtension': {}, 'CFBundleDisplayName': '$(APP_DISPLAY_NAME)'}, f)
                entitlement = widget / ('qa.entitlements' if suffix else 'pro.entitlements')
                with entitlement.open('wb') as f:
                    plistlib.dump({'groups': ['$(APP_GROUP)']}, f)
                ext, = extensions.discover(root, settings.get('PRODUCT_BUNDLE_IDENTIFIER'), project)
                self.assertEqual(ext.bundle_id, 'com.example.roda' + suffix + '.widget')
                self.assertEqual(ext.entitlements, entitlement)
                self.assertEqual(extensions._info_plist(ext, {})['CFBundleDisplayName'], info['name'])
                self.assertEqual(app.expand({'groups': ['$(APP_GROUP)']}, ext.settings)['groups'],
                                 ['group.roda.qa' if suffix else 'group.roda'])

    def test_custom_device_flavor(self):
        paths = {'/proc/12/cmdline': b'dart\0/sdk/flutter_tools.snapshot\0run\0--flavor\0qa\0'}
        with patch.object(build.os, 'getppid', return_value=12), patch.object(Path, 'read_bytes', lambda p: paths[str(p)]):
            self.assertEqual(build.flutter_run_flavor(), 'qa')
        paths['/proc/12/cmdline'] = b'dart\0/sdk/flutter_tools.snapshot\0run\0'
        with patch.object(build.os, 'getppid', return_value=12), patch.object(Path, 'read_bytes', lambda p: paths[str(p)]):
            self.assertIsNone(build.flutter_run_flavor())


if __name__ == '__main__':
    unittest.main()
