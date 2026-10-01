"""Optional real Docker gate: CAO_TEST_IMAGE must name a prepared immutable image."""
import os
import tempfile
import unittest
from pathlib import Path

from test_apply import apply


@unittest.skipUnless(os.environ.get('CAO_TEST_IMAGE'), 'Set CAO_TEST_IMAGE for the real Docker isolation gate')
class IsolationTests(unittest.TestCase):
    def test_real_worker_has_no_credentials_network_git_and_cannot_mutate_candidate(self):
        check = '''import os, socket
from pathlib import Path
assert os.getuid() != 0
assert os.statvfs('/').f_flag & os.ST_RDONLY
assert not Path('/work/.git').exists()
assert not Path('/work/.env').exists()
assert not any(k in os.environ for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY'))
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('external network is reachable')
Path('/work/marker').write_text('only the disposable copy changed')
'''
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'source'
            source.mkdir()
            (source / '.git').mkdir()
            (source / '.env').write_text('GH_TOKEN=never-mount-this')
            (source / 'check.py').write_text(check)
            result = apply.test_candidate(source, work, {
                'test_image': os.environ['CAO_TEST_IMAGE'], 'test_commands': [['python3', '/work/check.py']]})
            self.assertEqual('passed', result[0]['result'])
            self.assertFalse((source / 'marker').exists())
            self.assertFalse((work / 'test-source').exists())


if __name__ == '__main__':
    unittest.main()
