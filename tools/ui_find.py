#!/usr/bin/env python3
"""按文字在 uiautomator XML 里找控件，输出点击坐标中心。

排序：完全相等 > 前缀 > 包含；同级优先 clickable="true"。
用法：ui_find.py <ui.xml> ["文字"] [--all]
"""
import re, sys

def nodes(path):
    s = open(path, encoding='utf-8', errors='replace').read()
    for m in re.finditer(r'<node[^>]*>', s):
        t = m.group(0)
        def g(k):
            mm = re.search(k + r'="([^"]*)"', t)
            return mm.group(1) if mm else ''
        yield {'text': g('text'), 'bounds': g('bounds'), 'class': g('class'),
               'rid': g('resource-id'), 'desc': g('content-desc'), 'clickable': g('clickable')}

def center(b):
    m = re.findall(r'\[(\d+),(\d+)\]', b)
    if len(m) != 2:
        return None
    (x1, y1), (x2, y2) = [(int(a), int(c)) for a, c in m]
    return (x1 + x2) // 2, (y1 + y2) // 2

def main():
    if len(sys.argv) < 2:
        sys.exit('usage: ui_find.py <ui.xml> ["text"] [--all]')
    path = sys.argv[1]
    args = [a for a in sys.argv[2:] if not a.startswith('--')]
    show_all = '--all' in sys.argv
    ns = list(nodes(path))
    if not args:
        for n in ns:
            if n['text'].strip() or n['desc'].strip():
                print('%-42s %-14s %s' % (n['text'] or n['desc'], n['bounds'], n['class'].split('.')[-1]))
        return
    needle = args[0]
    ranked = []
    for n in ns:
        hay = n['text'] + ' ' + n['desc']
        if needle == n['text'] or needle == n['desc']:
            score = 0
        elif hay.startswith(needle):
            score = 1
        elif needle in hay:
            score = 2
        else:
            continue
        if n['clickable'] != 'true':
            score += 3
        ranked.append((score, n))
    if not ranked:
        sys.exit('not found: ' + needle)
    ranked.sort(key=lambda x: x[0])
    if show_all:
        for score, n in ranked:
            print('%d %-40s %-14s clickable=%s' % (score, n['text'] or n['desc'], n['bounds'], n['clickable']))
        return
    c = center(ranked[0][1]['bounds'])
    if not c:
        sys.exit('no bounds')
    print('%d %d' % c)

if __name__ == '__main__':
    main()
