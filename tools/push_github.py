#!/usr/bin/env python3
"""Create xiaoshouhehuoren/cloudphone on GitHub and push this repo.

Usage:
    set GH_TOKEN=ghp_xxx        (Windows cmd)
    export GH_TOKEN=ghp_xxx     (bash)
    PYTHONPATH=vendor python tools/push_github.py
"""
import json, os, subprocess, sys, urllib.request, urllib.error

ORG = 'xiaoshouhehuoren'
REPO = 'cloudphone'


def api(path, method='GET', token=None, body=None):
    url = 'https://api.github.com' + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'cloudphone-push')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or '{}')


def verify(token):
    st, d = api('/user', token=token)
    print('auth:', st, d.get('login'), d.get('name'))
    st2, d2 = api('/user/memberships/orgs/%s' % ORG, token=token)
    print('org membership:', st2, (d2.get('role') if isinstance(d2, dict) else d2))
    return st == 200 and st2 == 200


def main():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        sys.exit('missing GH_TOKEN (GitHub personal access token with repo scope)')
    if '--verify' in sys.argv:
        sys.exit(0 if verify(token) else 3)
    st, d = api('/repos/%s/%s' % (ORG, REPO), token=token)
    if st == 404:
        st, d = api('/orgs/%s/repos' % ORG, 'POST', token,
                    {'name': REPO, 'private': False,
                     'description': 'Cloud phone (redroid on RK3588): camera HAL, WeChat control, ops docs'})
        print('create repo:', st, d.get('full_name') or d)
    else:
        print('repo exists:', st, d.get('full_name'))
    if st >= 400:
        sys.exit('cannot create repo: %s %s' % (st, d))

    try:
        from dulwich import porcelain
    except ImportError:
        sys.exit('dulwich missing: run with PYTHONPATH=vendor (see docs/G98-踩坑全记录.md)')
    url = 'https://x-access-token:%s@github.com/%s/%s.git' % (token, ORG, REPO)
    print('pushing...')
    porcelain.push('.', url, 'refs/heads/master:refs/heads/master')
    print('done: https://github.com/%s/%s' % (ORG, REPO))


if __name__ == '__main__':
    main()