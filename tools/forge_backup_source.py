"""Owner-reviewed offline backup-to-root-source preparation; no target access."""
import os
from pathlib import Path

from forge_recovery_archive_hash import capture
from forge_recovery_restore_source import digest, opened, prepare as prepare_source, sha
from forge_target_journal import private_directory, read_record, write_record


def inputs(directory, acceptance_pin):
    directory = Path(directory).absolute()
    fd = private_directory(directory)
    try:
        accepted = read_record(fd, 'acceptance.json')
        plan = read_record(fd, 'plan.json')
    finally:
        os.close(fd)
    if not sha(acceptance_pin) or digest(accepted) != acceptance_pin:
        raise ValueError('Backup receipt differs from owner-reviewed pin')
    extent = plan.get('extent', {})
    # opened() checks receipt, geometry, permissions and the archive identity,
    # without confusing the placeholder root digest with byte verification.
    root = dict(offset=extent.get('offset_bytes'), bytes=extent.get('length_bytes'), sha256='0'*64)
    with opened(directory, root) as (_, binding):
        if binding['backup_plan_sha256'] != digest(plan):
            raise ValueError('Backup plan changed during review')
        if binding['backup_acceptance_sha256'] != acceptance_pin:
            raise ValueError('Backup receipt changed during review')
    return dict(directory=str(directory), acceptance_sha256=acceptance_pin,
                plan_sha256=digest(plan), archive_fingerprint=binding['archive_fingerprint'],
                card=accepted['card'], root_offset=root['offset'], root_bytes=root['bytes'])


def prepare(output, reviewed):
    """Read twice: derive independent range digests, then verify chunk mapping."""
    if inputs(reviewed['directory'], reviewed['acceptance_sha256']) != reviewed:
        raise ValueError('Backup changed since owner review')
    output = Path(output).absolute()
    source = Path(reviewed['directory']).resolve(strict=True)
    resolved = output.resolve()
    if resolved == source or source in resolved.parents or resolved in source.parents:
        raise ValueError('Preparation output must not overlap the retained backup')
    output.mkdir(mode=0o700)
    parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    fd = private_directory(output)
    try:
        write_record(fd, 'request.json', reviewed)
        ranges = capture(source, output/'ranges')
        mapped = prepare_source(source, output/'source', ranges['digests']['root'])
        if inputs(reviewed['directory'], reviewed['acceptance_sha256']) != reviewed:
            raise ValueError('Backup changed during source preparation')
        result = dict(status='prepared-backup-root-source',
            backup_acceptance_sha256=reviewed['acceptance_sha256'],
            backup_plan_sha256=reviewed['plan_sha256'],
            ranges_sha256=digest(ranges), manifest_sha256=mapped['manifest_sha256'],
            root=ranges['digests']['root'], chunk_count=mapped['chunk_count'],
            target_contacted=False, target_written=False, lease_acquired=False,
            filesystem_consistency_qualified=False, restore_authorized=False,
            normal_boot_release_authorized=False)
        write_record(fd, 'acceptance.json', result)
        return result
    except BaseException as exc:
        write_record(fd, 'failure.json', dict(error_type=type(exc).__name__,
            target_contacted=False, target_written=False, preserve_evidence=True))
        raise
    finally:
        os.close(fd)
