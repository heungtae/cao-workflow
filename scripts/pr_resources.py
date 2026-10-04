"""Identity of the PR handler's actual dependencies, never the whole manifest."""
import hashlib
import json
from pathlib import Path

import manage

NAMES = ('github-pr-review', 'github-pr-apply')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identity():
    manifest = manage.load_manifest()
    workflows = [w for w in manifest['workflows'] if w['name'] in NAMES]
    agents = {a for w in workflows for a in w['agents']}
    configs = [manage.codex_review_profile_path(), manage.codex_apply_profile_path()]
    resources = []
    for kind, entries in [('workflows', workflows), ('agents', [a for a in manifest['agents'] if a['name'] in agents])]:
        for entry in entries:
            resources.append({'kind': kind, 'name': entry['name'], 'version': entry.get('version'),
                              'sha256': manage.digest(manage.ROOT / entry['source'])})
    for path in configs:
        resources.append({'kind': 'codex', 'name': path.name, 'sha256': manage.digest(path)})
    return resources


def guard_files():
    """Trusted paths copied into the private execution journal for child gates."""
    home, manifest = manage.cao_home(), manage.load_manifest()
    workflows = [w for w in manifest['workflows'] if w['name'] in NAMES]
    agents = {a for w in workflows for a in w['agents']}
    paths = []
    for kind, entries in [('workflows', workflows), ('agents', [a for a in manifest['agents'] if a['name'] in agents])]:
        for entry in entries:
            paths += [manage.ROOT / entry['source'], manage.target(home, kind, entry)]
            if kind == 'agents':
                paths.append(manage.profile_context(home, entry['name']))
    paths += [manage.codex_review_profile_path(), manage.codex_apply_profile_path()]
    if any(any(x.is_symlink() for x in (p, *p.parents)) for p in paths):
        raise ValueError('PR dependencies cannot use symlink paths')
    return {str(p.absolute()): manage.digest(p) for p in paths}
