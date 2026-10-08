"""Static contract only: never execute the Windows updater/remote actions."""
import unittest
from pathlib import Path


class PortableUpdateTransactionTests(unittest.TestCase):
    def test_transaction_contract(self):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'update_portable_local.ps1'
        self.assertTrue(path.exists(), 'reviewed local updater missing')
        script = path.read_text()
        self.assertIn('IO.DriveInfo', script)
        self.assertIn('$drive.AvailableFreeSpace', script)
        self.assertIn('$available -isnot [long]', script)
        self.assertNotIn('Get-PSDrive', script)
        self.assertNotIn('$drive.Free', script)
        for requirement in ('AssertSafeTree', 'AssertManifest', '$incomingManifest', '$backupManifest',
                            '$touched', '$rollbackErrors', 'quarantine', 'protected', 'SHA256'):
            self.assertIn(requirement, script)
        self.assertNotIn('Remove-Item', script)
        self.assertNotIn('Invoke-WebRequest', script)
        self.assertNotIn('Move-Item -LiteralPath $saved -Destination $dest', script)
        self.assertLess(script.index('AssertManifest $backup $backupManifest'), script.index('# BEGIN REPLACE'))
        self.assertLess(script.index('AssertManifest $bundle $incomingManifest'), script.index('# BEGIN REPLACE'))
        self.assertIn('AssertManifest $root $incomingManifest', script)
        self.assertIn('AssertManifest $root $originalManifest', script)
        self.assertIn('installed=$false', script)
        self.assertIn('installed=$true', script)
