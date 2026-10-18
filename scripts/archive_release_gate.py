"""Fail-closed policy; this phase has no Release publishing capability."""
import re
ARCHIVE_REPOSITORY='donozoyout-ux/ma-larii-archive'


def validate_destination(repository,exporter_commit,publish_release=False):
    if (repository.get('full_name')!=ARCHIVE_REPOSITORY or repository.get('private') is not True
            or repository.get('visibility')!='private'):
        raise ValueError('Archive destination must be the approved private repository')
    if not isinstance(exporter_commit,str) or not re.fullmatch('[a-f0-9]{40}',exporter_commit):
        raise ValueError('Exporter must be pinned to a vetted immutable commit')
    if publish_release:
        raise ValueError('Release publishing disabled pending source-rights clearance and separate reviewed uploader')
