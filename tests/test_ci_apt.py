"""CI mirror routing, unchanged signed sources and bootstrap refusal controls."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
HELPER = (HERE.parent / 'tools/ci_apt.sh'
          if (HERE.parent / 'tools/ci_apt.sh').exists() else HERE / 'ci-apt.sh')


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'POSIX bash required')
class AptBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci apt fixtures ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / 'apt'
        (self.sources / 'sources.list.d').mkdir(parents=True)
        self.ubuntu = self.sources / 'sources.list.d/ubuntu.sources'
        self.ubuntu.write_text(
            'Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n'
            'Suites: noble noble-updates noble-backports\n'
            'Components: main restricted universe multiverse\n'
            'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\n'
            'Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors-security.txt\n'
            'Suites: noble-security\nComponents: main universe\n'
            'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        self.other = self.sources / 'sources.list.d/other.list'
        self.other.write_text('deb [signed-by=/keys/other.gpg] https://example.invalid/packages stable main\n')
        self.original = {p.relative_to(self.sources).as_posix(): p.read_bytes()
                         for p in self.sources.rglob('*') if p.is_file()}
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'trace.jsonl'
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        mock = self.root / 'mock_apt.py'
        mock.write_text(
            'import json,os,pathlib,sys\n'
            'args=sys.argv[1:];assert args[0]=="apt-get",args\n'
            'prior=[json.loads(x) for x in pathlib.Path(os.environ["APT_TRACE"]).read_text().splitlines()] if pathlib.Path(os.environ["APT_TRACE"]).exists() else []\n'
            'opts=[args[i+1] for i,x in enumerate(args) if x=="-o"]\n'
            'main=pathlib.Path(next(x.split("=",1)[1] for x in opts if x.startswith("Dir::Etc::sourcelist=")))\n'
            'parts=pathlib.Path(next(x.split("=",1)[1] for x in opts if x.startswith("Dir::Etc::sourceparts=")))\n'
            'record={"args":args,"main":main.read_text(),"parts":{p.name:p.read_text() for p in parts.iterdir()},"temporary":str(main.parent)}\n'
            'with open(os.environ["APT_TRACE"],"a") as f:f.write(json.dumps(record)+"\\n")\n'
            'phase="UPDATE" if "update" in args else "INSTALL"\n'
            'code=int(os.environ.get("FAIL_"+phase,"0"))\n'
            'if phase=="UPDATE" and not any("update" in row["args"] for row in prior):code=int(os.environ.get("FAIL_UPDATE_ONCE",str(code)))\n'
            'if code and os.environ.get("FAKE_APT_ERROR"):print(os.environ["FAKE_APT_ERROR"],file=sys.stderr)\n'
            'raise SystemExit(code)\n')
        commands = {
            'uname': '#!/bin/sh\nprintf "%s\\n" "${FAKE_KERNEL:-Linux}"\n',
            'dpkg': '#!/bin/sh\nprintf "%s\\n" "${FAKE_ARCH:-amd64}"\n',
            'sudo': '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                    shlex.quote(str(mock)) + ' "$@"\n',
        }
        for name, data in commands.items():
            file = self.bin / name
            file.write_text(data)
            file.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted',
                        CI_APT_SOURCE_ROOT=str(self.sources), APT_TRACE=str(self.trace),
                        TMPDIR=str(self.scratch))

    def run_helper(self, args=('lua5.4',), **env):
        result = subprocess.run([shutil.which('bash'), str(HELPER), *args],
                                env=dict(self.env, **env), capture_output=True, text=True)
        records = [json.loads(x) for x in self.trace.read_text().splitlines()] if self.trace.exists() else []
        self.assertEqual(self.original, {p.relative_to(self.sources).as_posix(): p.read_bytes()
                                       for p in self.sources.rglob('*') if p.is_file()})
        self.assertEqual(list(self.scratch.iterdir()), [])
        return result, records

    def test_signed_deb822_and_other_feeds_preserve_packages_and_keys(self):
        result, records = self.run_helper(('lua5.4=5.4.6-3build2', '--no-install-recommends'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(records), 2)
        ubuntu = records[0]['parts']['ubuntu.sources']
        self.assertIn('URIs: https://archive.ubuntu.com/ubuntu\n', ubuntu)
        self.assertIn('URIs: https://security.ubuntu.com/ubuntu\n', ubuntu)
        self.assertIn('Suites: noble noble-updates noble-backports\n', ubuntu)
        self.assertEqual(ubuntu.count('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg'), 2)
        self.assertEqual(records[0]['parts']['other.list'], self.other.read_text())
        self.assertIn('APT::Update::Error-Mode=any', records[0]['args'])
        self.assertEqual(records[1]['args'][-2:], ['lua5.4=5.4.6-3build2', '--no-install-recommends'])

    def test_arm_uses_official_ports_archive(self):
        result, records = self.run_helper(FAKE_ARCH='arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(records[0]['parts']['ubuntu.sources'].count('https://ports.ubuntu.com/ubuntu-ports'), 2)
        self.assertNotIn('https://archive.ubuntu.com/ubuntu', records[0]['parts']['ubuntu.sources'])

    def test_legacy_source_format_and_direct_azure_uri(self):
        file = self.sources / 'sources.list'
        file.write_text('deb [arch=amd64 signed-by=/keys/ubuntu.gpg] http://azure.archive.ubuntu.com/ubuntu jammy main universe\n')
        self.original[file.relative_to(self.sources).as_posix()] = file.read_bytes()
        result, records = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(records[0]['main'], 'deb [arch=amd64 signed-by=/keys/ubuntu.gpg] https://archive.ubuntu.com/ubuntu jammy main universe\n')

    def test_local_self_hosted_and_non_linux_refused_before_apt(self):
        for env in ({'GITHUB_ACTIONS': 'false'}, {'RUNNER_ENVIRONMENT': 'self-hosted'},
                    {'RUNNER_ENVIRONMENT': ''}, {'FAKE_KERNEL': 'Darwin'}):
            with self.subTest(env=env):
                result, records = self.run_helper(**env)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(records, [])

    def test_unknown_architecture_refused(self):
        result, records = self.run_helper(FAKE_ARCH='unknown')
        self.assertEqual(result.returncode, 64)
        self.assertEqual(records, [])

    def test_update_failure_prevents_install(self):
        result, records = self.run_helper(FAIL_UPDATE='100')
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)

    def test_install_failure_propagates(self):
        result, records = self.run_helper(FAIL_INSTALL='100')
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 2)

    def test_empty_package_request_refused(self):
        result, records = self.run_helper(args=())
        self.assertEqual(result.returncode, 64)
        self.assertEqual(records, [])


    def test_ubuntu_dns_failure_retries_same_signed_archive(self):
        result, records = self.run_helper(
            FAIL_UPDATE_ONCE='100', FAKE_APT_ERROR="Temporary failure resolving 'archive.ubuntu.com'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(records), 3)
        self.assertIn('https://archive.ubuntu.com/ubuntu', records[0]['parts']['ubuntu.sources'])
        self.assertEqual(records[1]['parts']['ubuntu.sources'].count('https://mirrors.kernel.org/ubuntu'), 2)
        self.assertEqual(records[1]['parts']['other.list'], self.other.read_text())
        self.assertEqual(records[1]['parts']['ubuntu.sources'].count('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg'), 2)

    def test_signature_failure_is_not_retried(self):
        result, records = self.run_helper(FAIL_UPDATE='100', FAKE_APT_ERROR='NO_PUBKEY: signature verification failed')
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)

    def test_other_feed_dns_failure_is_not_retried(self):
        result, records = self.run_helper(FAIL_UPDATE='100', FAKE_APT_ERROR="Temporary failure resolving 'example.invalid'")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)

    def test_failed_mirror_preserves_error(self):
        result, records = self.run_helper(FAIL_UPDATE='100', FAKE_APT_ERROR="Temporary failure resolving 'archive.ubuntu.com'")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 2)
        self.assertFalse(any('install' in row['args'] for row in records))

    def test_arm_dns_failure_never_uses_amd64_mirror(self):
        result, records = self.run_helper(FAKE_ARCH='arm64', FAIL_UPDATE='100', FAKE_APT_ERROR="Temporary failure resolving 'ports.ubuntu.com'")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)

    def test_missing_sources_refused(self):
        self.ubuntu.unlink()
        self.other.unlink()
        self.original = {}
        result, records = self.run_helper()
        self.assertEqual(result.returncode, 64)
        self.assertEqual(records, [])



    def test_mixed_dns_and_signature_failure_is_not_retried(self):
        result, records = self.run_helper(FAIL_UPDATE='100', FAKE_APT_ERROR="Temporary failure resolving 'archive.ubuntu.com'\\nNO_PUBKEY")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)

    def test_mixed_dns_and_expired_metadata_is_not_retried(self):
        result, records = self.run_helper(FAIL_UPDATE='100', FAKE_APT_ERROR="Temporary failure resolving 'archive.ubuntu.com'\\nRelease file example is expired")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(len(records), 1)



if __name__ == '__main__':
    unittest.main()
