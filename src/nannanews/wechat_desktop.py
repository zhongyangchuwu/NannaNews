"""Read-only public-account discovery in the separately authorized Linux client.

The client supplies discovery URLs, not synthetic HTTP responses. Originals and
all media still use acquisition.fetch_document and the existing capture pipeline.
"""
from collections import deque
from contextlib import contextmanager
import csv
from datetime import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time

from PIL import Image

from nannanews import acquisition, wechat_public


_CONTAINER = 'nannanews-wechat-desktop-desktop-1'
_IMAGE = 'nannanews-wechat-desktop:4.1.13.26'
_XCLIP = '/usr/bin/xclip'
_RUNTIME = '/tmp/wechat-desktop'
_PUBLIC_EXE = '/opt/wechat/RadiumWMPF/runtime/WeChatAppEx'
_MARKER = 'NANNANEWS_PUBLIC_CLIPBOARD_CLEARED'
_CAPTURE_CODE = Path(__file__).with_name('wechat_window_capture.py').read_text(encoding='utf-8')


def _blocked(message='专用微信公开界面不可读取；请人工检查登录、验证或客户端状态。'):
    return acquisition.AcquisitionError(message, 'source_blocked')


def _command(arguments, *, data=None, timeout=15, discard=False):
    try:
        result = subprocess.run(arguments, input=data, stdout=subprocess.DEVNULL if discard else subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise _blocked() from None
    if result.returncode:
        raise _blocked()
    return b'' if discard else result.stdout


def _exec(*arguments, data=None, timeout=15, discard=False):
    # Killing only the Docker CLI would leave its guest process running.
    duration = f'{max(0.05, timeout - 0.25):.3f}s'
    return _command(['docker', 'exec', *(['-i'] if data is not None else []), _CONTAINER,
                     'timeout', '--kill-after=0.1s', duration, *arguments],
                    data=data, timeout=timeout, discard=discard)


@contextmanager
def _desktop_lock():
    # Unlike the per-database daily lock, this also serializes isolated smoke DBs.
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'nannanews/wechat-desktop'
    if directory.is_symlink():
        raise _blocked()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(directory / 'collector.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _blocked('专用微信界面正在被另一个采集任务使用；未并发操作。') from None
        yield
    finally:
        os.close(descriptor)


def _normalize(text):
    return re.sub(r'\s+', '', text)


def _extent(reply):
    match = re.search(r'\(\((-?\d+), (-?\d+), (\d+), (\d+)\)', reply)
    if not match:
        raise _blocked()
    return tuple(map(int, match.groups()))


def _children(reply):
    return re.findall(r"\('([^']+)', (?:objectpath )?'(/org/a11y/atspi/accessible/[^']+)'\)", reply)


class _PublicDesktop:
    def __init__(self, deadline=None):
        self.end = time.monotonic() + 180
        if deadline is not None:
            self.end = min(self.end, time.monotonic() + max(0, (deadline - datetime.now(deadline.tzinfo)).total_seconds()))
        self.window = None
        self.geometry = None
        self.phase = None
        self.account_name = None
        self.accessibility = None
        self.authorized = False
        self.clipboard_owned = False
        self.clipboard_owner = None
        self.browser_owned = False
        self.manual_halt = False

    def check_time(self):
        if time.monotonic() >= self.end:
            raise _blocked('专用微信公开发现达到本轮时间上限；已发现链接保留，未宣称完整覆盖。')


    def command(self, *arguments, data=None, timeout=15, discard=False):
        self.check_time()
        return _exec(*arguments, data=data, discard=discard,
                     timeout=max(0.05, min(timeout, self.end - time.monotonic())))

    def verify_runtime(self):
        self.check_time()
        raw = _command(['docker', 'inspect', '--format',
                        '{{json .Config.Labels}} {{.HostConfig.ReadonlyRootfs}} {{.Config.User}} {{.State.Status}}', _CONTAINER],
                       timeout=max(0.05, min(10, self.end - time.monotonic()))).decode()
        labels, end = json.JSONDecoder().raw_decode(raw)
        state = raw[end:].strip().split()
        if (not isinstance(labels, dict) or labels.get('com.docker.compose.project') != 'nannanews-wechat-desktop'
                or labels.get('com.docker.compose.service') != 'desktop'
                or len(state) != 3 or state[:2] != ['true', '10001:10001']):
            raise _blocked('拒绝操作未通过隔离配置核验的微信容器。')
        if state[2] != 'running':
            raise acquisition.AcquisitionError('专用微信容器当前未运行或不可执行；请启动现有专用桌面，未读取账号内容。',
                                               'source_runtime_unavailable')
        self.authorized = True

    def geometry_for(self, window):
        text = self.command('xdotool', 'getwindowgeometry', '--shell', str(window)).decode()
        values = dict(re.findall(r'^(X|Y|WIDTH|HEIGHT)=(-?\d+)$', text, re.M))
        if len(values) != 4:
            raise _blocked()
        return tuple(int(values[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT'))

    def is_public(self, window):
        text = self.command('xprop', '-id', str(window), 'WM_CLASS', '_NET_WM_PID').decode()
        if not re.search(r'WM_CLASS\(STRING\) = "com\.tencent\.wechatappex", "WeChatAppEx"', text):
            return False
        match = re.search(r'_NET_WM_PID\(CARDINAL\) = (\d+)', text)
        return bool(match and self.command('readlink', '-f', '/proc/' + match[1] + '/exe').decode().strip() == _PUBLIC_EXE)

    def guard(self):
        self.check_time()
        if (self.window is None or self.command('xdotool', 'getwindowfocus').decode().strip() != str(self.window)
                or not self.is_public(self.window)):
            self.manual_halt = True
            raise _blocked('公开微信窗口失去焦点或所有权；已停止，未转向聊天窗口。')
        current = self.geometry_for(self.window)
        if self.geometry != current:
            self.manual_halt = True
            raise _blocked('公开微信窗口位置或尺寸发生变化；已停止使用旧坐标。')

    def a11y(self, bus, path, method, *arguments):
        address = ['--address', self.accessibility] if self.accessibility else ['--session']
        return self.command('gdbus', 'call', *address, '--dest', bus, '--object-path', path,
                            '--method', method, *arguments, timeout=5).decode()

    def open_search(self):
        self.check_time()
        self.verify_runtime()
        pid = self.command('cat', _RUNTIME + '/wechat.pid').decode().strip()
        if not pid.isdecimal() or self.command('readlink', '-f', '/proc/' + pid + '/exe').decode().strip() != '/opt/wechat/wechat':
            raise _blocked()
        windows = self.command('xdotool', 'search', '--onlyvisible', '--pid', pid).decode().split()
        candidates = [(window, self.geometry_for(window)) for window in windows]
        candidates = [(window, bounds) for window, bounds in candidates if bounds[2] >= 600 and bounds[3] >= 400]
        if len(candidates) != 1:
            raise _blocked('未找到唯一的专用微信主窗口；请先完成登录或关闭验证弹窗。')
        main_window, physical = candidates[0]
        reply = self.a11y('org.a11y.Bus', '/org/a11y/bus', 'org.a11y.Bus.GetAddress')
        address = re.search(r"'([^']+)'", reply)
        if not address:
            raise _blocked()
        self.accessibility = address[1]
        apps = _children(self.a11y('org.a11y.atspi.Registry', '/org/a11y/atspi/accessible/root',
                                   'org.a11y.atspi.Accessible.GetChildren'))
        native = []
        for bus, path in apps:
            self.check_time()
            name = self.a11y(bus, path, 'org.freedesktop.DBus.Properties.Get', 'org.a11y.atspi.Accessible', 'Name')
            if re.search(r"<'(?:wechat|Weixin|微信)'>", name):
                native.extend(_children(self.a11y(bus, path, 'org.a11y.atspi.Accessible.GetChildren')))
        search = None
        for bus, frame in native:
            logical = _extent(self.a11y(bus, frame, 'org.a11y.atspi.Component.GetExtents', '0'))
            if logical[2] < 500 or logical[3] < 300:
                continue
            queue = deque([(bus, frame, 0)])
            visited = set()
            while queue and len(visited) < 80:
                self.check_time()
                child_bus, child_path, depth = queue.popleft()
                if (child_bus, child_path) in visited or depth > 12:
                    continue
                visited.add((child_bus, child_path))
                bounds = _extent(self.a11y(child_bus, child_path, 'org.a11y.atspi.Component.GetExtents', '0'))
                # Never traverse or name the contacts/chat/content pane to the right.
                if bounds[2] <= 0 or bounds[3] <= 0 or not logical[0] <= bounds[0] <= logical[0] + 8:
                    continue
                if bounds[2] <= 80 and bounds[3] <= 60:
                    name = self.a11y(child_bus, child_path, 'org.freedesktop.DBus.Properties.Get',
                                      'org.a11y.atspi.Accessible', 'Name')
                    if re.search(r"<'(?:Search|搜一搜)'>", name):
                        search = (bounds, logical, child_bus, child_path, bus, frame)
                        break
                queue.extend((b, p, depth + 1) for b, p in
                             _children(self.a11y(child_bus, child_path, 'org.a11y.atspi.Accessible.GetChildren')))
            if search:
                break
        if not search:
            raise _blocked('专用微信未导出可核验的“搜一搜”导航；请检查登录，未读取聊天内容。')
        bounds, logical, control_bus, control_path, frame_bus, frame_path = search
        self.check_time()
        self.command('xdotool', 'windowactivate', '--sync', main_window)
        metadata = self.command('xprop', '-id', main_window, '_NET_WM_PID').decode()
        if (not re.search(r'_NET_WM_PID\(CARDINAL\) = ' + re.escape(pid) + r'\s*$', metadata)
                or self.command('readlink', '-f', '/proc/' + pid + '/exe').decode().strip() != '/opt/wechat/wechat'
                or self.geometry_for(main_window) != physical
                or _extent(self.a11y(frame_bus, frame_path, 'org.a11y.atspi.Component.GetExtents', '0')) != logical
                or _extent(self.a11y(control_bus, control_path, 'org.a11y.atspi.Component.GetExtents', '0')) != bounds):
            raise _blocked('微信主窗口或“搜一搜”位置在定位期间变化；未使用旧坐标。')
        x = round(physical[0] + (bounds[0] + bounds[2] / 2 - logical[0]) * physical[2] / logical[2])
        y = round(physical[1] + (bounds[1] + bounds[3] / 2 - logical[1]) * physical[3] / logical[3])
        self.command('xdotool', 'mousemove', str(x), str(y), 'click', '1')
        for _ in range(20):
            self.check_time()
            focus = self.command('xdotool', 'getwindowfocus').decode().strip()
            if focus.isdecimal() and self.is_public(focus):
                self.window = focus
                self.geometry = self.geometry_for(focus)
                if not (700 <= self.geometry[2] <= 1440 and 600 <= self.geometry[3] <= 1000):
                    raise _blocked()
                self.phase = 'search'
                self.browser_owned = True
                return
            time.sleep(0.2)
        raise _blocked()

    def click(self, x, y):
        self.guard()
        if not (0 <= x < self.geometry[2] and 0 <= y < self.geometry[3]):
            raise _blocked()
        self.command('xdotool', 'mousemove', str(self.geometry[0] + round(x)),
                     str(self.geometry[1] + round(y)), 'click', '1')

    def key(self, key):
        self.guard()
        self.command('xdotool', 'key', key)

    def clipboard(self, text):
        if not self.authorized:
            raise _blocked()
        self.command(_XCLIP, '-selection', 'clipboard', '-in', data=text.encode(), discard=True)
        owner = self.command('python3', '-c', _CAPTURE_CODE, '--owner', str(self.window), timeout=5).decode().strip()
        if not owner.isdecimal() or owner == '0':
            raise _blocked('无法核验公开操作的剪贴板选择区；未读取剪贴板。')
        self.clipboard_owner = owner
        self.clipboard_owned = True

    def clear_clipboard(self):
        if self.authorized and self.clipboard_owned and self.clipboard_owner:
            try:
                _exec('python3', '-c', _CAPTURE_CODE, '--clear', str(self.window), self.clipboard_owner, timeout=3)
                self.clipboard_owned = False
                self.clipboard_owner = None
            except acquisition.AcquisitionError:
                pass

    def cleanup(self):
        self.clear_clipboard()
        if self.authorized and self.browser_owned and not self.manual_halt:
            deadline = self.end
            self.end = time.monotonic() + 5
            try:
                self.guard()
                self.command('python3', '-c', _CAPTURE_CODE, '--close', str(self.window), timeout=3)
                self.browser_owned = False
            except acquisition.AcquisitionError:
                pass
            finally:
                self.end = deadline

    def _surface_image(self, window, bounds, *, menu=False):
        self.guard()
        arguments = ['--menu', str(self.window), str(window)] if menu else [str(window)]
        captured = self.command('python3', '-c', _CAPTURE_CODE, *arguments, timeout=10)
        self.guard()
        if len(captured) < 12:
            raise _blocked()
        width, height, stride = struct.unpack('!III', captured[:12])
        if ((width, height) != bounds[2:] or not width * 4 <= stride <= width * 4 + 256
                or len(captured) != 12 + stride * height):
            raise _blocked()
        return Image.frombytes('RGB', (width, height), captured[12:], 'raw', 'BGRX', stride, 1)

    def screen(self):
        return self._surface_image(self.window, self.geometry)

    def lines(self):
        return self._recognize(self.screen())

    def menu_lines(self):
        self.guard()
        metadata = self.command('xprop', '-id', str(self.window), '_NET_WM_PID').decode()
        match = re.search(r'_NET_WM_PID\(CARDINAL\) = (\d+)', metadata)
        if not match:
            raise _blocked()
        pid = match[1]
        windows = self.command('xdotool', 'search', '--onlyvisible', '--pid', pid).decode().split()
        menus = []
        for window in windows:
            if window == str(self.window):
                continue
            properties = self.command('xprop', '-id', window, '_NET_WM_PID', '_NET_WM_WINDOW_TYPE').decode()
            if re.search(r'_NET_WM_WINDOW_TYPE\(ATOM\) = _NET_WM_WINDOW_TYPE_MENU\s*$', properties, re.M):
                bounds = self.geometry_for(window)
                x, y, w, h = bounds
                left, top, width, height = self.geometry
                if (not re.search(r'_NET_WM_PID\(CARDINAL\) = ' + re.escape(pid) + r'\s*$', properties, re.M)
                        or not (left <= x and top <= y and 0 < w <= width and 0 < h <= height
                                and x + w <= left + width and y + h <= top + height)):
                    raise _blocked('公开菜单的发布进程或位置无法核验；未读取菜单。')
                menus.append((window, bounds))
        if not menus:
            return []
        if len(menus) != 1:
            raise _blocked('出现多个公开菜单；已停止，未猜测操作对象。')
        window, bounds = menus[0]
        lines = self._recognize(self._surface_image(window, bounds, menu=True))
        for line in lines:
            for key in ('left', 'right', 'x'):
                line[key] += bounds[0] - self.geometry[0]
            for key in ('top', 'bottom', 'y'):
                line[key] += bounds[1] - self.geometry[1]
            line['surface_id'], line['surface_bounds'] = window, bounds
        return lines

    def _recognize(self, image):
        resized = image.resize((image.width * 2, image.height * 2))
        with io.BytesIO() as stream:
            resized.save(stream, format='PNG')
            png = stream.getvalue()
        self.check_time()
        budget = max(0.05, min(30, self.end - time.monotonic()))
        duration = f'{max(0.05, budget - 0.25):.3f}s'
        data = _command(['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                         '--memory', '256m', '--cpus', '1', '--pids-limit', '32',
                         '--tmpfs', '/tmp:size=16m,mode=1777', '--entrypoint', 'timeout',
                         _IMAGE, '--kill-after=0.1s', duration, 'tesseract',
                         'stdin', 'stdout', '-l', 'chi_sim+eng', '--psm', '11', 'tsv'],
                        data=png, timeout=budget).decode()
        groups = {}
        for word in csv.DictReader(io.StringIO(data), delimiter='\t'):
            if word['text'].strip() and float(word['conf']) >= 35:
                groups.setdefault((word['block_num'], word['par_num'], word['line_num']), []).append(word)
        groups_for_navigation = list(groups.values())
        labels = set()
        if self.phase == 'profile':
            labels.add('文章')
        elif self.phase == 'account_results':
            labels.add('OfficialAccounts')
            if self.account_name:
                labels.add(self.account_name)
        # Preserve complete lines for challenge detection. Navigation may use
        # exact contiguous word spans, never a merged badge's guessed center.
        for words in groups.values() if labels else ():
            for start in range(len(words)):
                text = ''
                for end in range(start, len(words)):
                    text += _normalize(words[end]['text'])
                    label = text.strip('“”‘’\"\'') if self.phase == 'profile' else text
                    if not any(target.startswith(label) for target in labels):
                        break
                    if label in labels and (start or end < len(words) - 1):
                        groups_for_navigation.append(words[start:end + 1])
        lines = []
        for words in groups_for_navigation:
            left = min(int(w['left']) for w in words) / 2
            top = min(int(w['top']) for w in words) / 2
            right = max(int(w['left']) + int(w['width']) for w in words) / 2
            bottom = max(int(w['top']) + int(w['height']) for w in words) / 2
            lines.append({'text': ''.join(w['text'] for w in words), 'left': left, 'top': top,
                          'right': right, 'bottom': bottom, 'x': (left + right) / 2, 'y': (top + bottom) / 2})
        if any(re.search(r'请完成.{0,5}验证|环境异常|扫码登录|操作频繁', line['text']) for line in lines):
            self.manual_halt = True
            raise acquisition.AcquisitionError('专用微信出现登录或人工验证提示；已停止，不自动处理或绕过。',
                                               'source_verification_required')
        return lines

    def wait_lines(self, predicate):
        ready_until = min(self.end, time.monotonic() + 45)
        while time.monotonic() < ready_until:
            self.check_time()
            lines = self.lines()
            if predicate(lines):
                return lines
            time.sleep(0.25)
        raise _blocked(f'公开微信页面（{self.phase}）未在限定等待内达到已验证状态；未继续盲点。')

    def choose(self, lines, predicate):
        matches = [line for line in lines if predicate(line)]
        if not matches:
            raise _blocked()
        line = min(matches, key=lambda item: (item['top'], item['left']))
        if line.get('surface_id') and self.geometry_for(line['surface_id']) != line['surface_bounds']:
            raise _blocked('公开菜单在读取后移动；未使用旧坐标。')
        self.click(line['x'], line['y'])
        return line

    def search_account(self, name):
        self.account_name = _normalize(name)
        self.phase = 'search_home'
        lines = self.wait_lines(lambda rows: any('OfficialAccounts' in _normalize(r['text']) and r['top'] > 100 for r in rows))
        # Only the pinned client's verified public search homepage uses this layout.
        official = next(r for r in lines if 'OfficialAccounts' in _normalize(r['text']) and r['top'] > 100)
        self.click(self.geometry[2] / 2 - 100, official['top'] - 45)
        self.clipboard(name)
        self.key('ctrl+a')
        self.key('ctrl+v')
        self.key('Return')
        self.phase = 'search_results'
        lines = self.wait_lines(lambda rows: any(_normalize(r['text']) == 'Accounts' and 70 < r['top'] < 180 for r in rows))
        self.choose(lines, lambda r: _normalize(r['text']) == 'Accounts' and 70 < r['top'] < 180)
        self.phase = 'account_results'
        normalized = self.account_name
        def official_filter(row):
            return _normalize(row['text']) == 'OfficialAccounts' and 150 < row['top'] < 240

        lines = self.wait_lines(lambda rows: sum(official_filter(r) for r in rows) == 1)
        self.choose(lines, official_filter)

        def account(row):
            return 150 < row['top'] < 650 and _normalize(row['text']) == normalized

        lines = self.wait_lines(lambda rows: sum(account(r) for r in rows) == 1)
        self.choose(lines, account)
        self.phase = 'profile'
        def is_article_tab(row):
            return _normalize(row['text']).strip('“”‘’\"\'') == '文章' and 180 < row['top'] < 450

        lines = self.wait_lines(lambda rows: any(_normalize(r['text']) == normalized and 45 < r['top'] < 160 for r in rows)
                                and any(is_article_tab(r) for r in rows))
        article_tab = self.choose(lines, is_article_tab)
        return article_tab['bottom'] + 12

    def copy_article_url(self):
        # Copy Link becomes available when article navigation commits; a gray
        # publication label or slow images are not evidence for URL readiness.
        self.phase = 'article_menu'
        ready_until = self.end
        try:
            while time.monotonic() < ready_until:
                self.lines()  # Detect manual verification before any menu input.
                self.clipboard(_MARKER)
                self.click(self.geometry[2] - 127, 22)
                lines = self.menu_lines()
                if any(_normalize(row['text']) == 'CopyLink' for row in lines):
                    self.choose(lines, lambda row: _normalize(row['text']) == 'CopyLink')
                    self.lines()  # Halt on a visible manual-verification page.
                    self.guard()
                    try:
                        packet = self.command('python3', '-c', _CAPTURE_CODE, '--clipboard',
                                              str(self.window), self.clipboard_owner, timeout=5)
                    except acquisition.AcquisitionError:
                        self.manual_halt = True
                        raise _blocked('公开复制操作的剪贴板拥有者无法核验；未读取其他程序的内容。') from None
                    if packet:
                        if len(packet) < 4:
                            raise _blocked()
                        self.clipboard_owner = str(struct.unpack('!I', packet[:4])[0])
                        try:
                            copied = packet[4:].decode().strip()
                        except UnicodeError:
                            raise _blocked('公开文章链接编码不可读取。') from None
                        return wechat_public.article_url(copied)
                self.key('Escape')
                time.sleep(0.25)
            raise _blocked('文章原文链接未在限定时间内就绪；未猜测链接或读取旧剪贴板。')
        finally:
            self.clear_clipboard()


def discover_articles(definition, *, page_limit, deadline=None):
    """Discover a bounded visible timeline; never claim the entire account archive."""
    wechat_public.validate_definition(definition)
    if type(page_limit) is not int or page_limit < 1:
        raise ValueError('GUI discovery page limit must be positive.')
    links, visited_titles, seen_urls = [], set(), set()
    halt_required = False
    limit = min(page_limit * 4, 24)
    views = min(page_limit * 3, 18)
    message = '专用微信公开时间线已按界面与文章上限扫描；不代表账号全量历史覆盖。'
    with _desktop_lock():
        desktop = _PublicDesktop(deadline)
        try:
            desktop.open_search()
            content_top = desktop.search_account(definition['expected_identity'])
            for view in range(views):
                desktop.check_time()
                lines = desktop.lines()
                candidates = [line for line in lines if content_top <= line['top'] < desktop.geometry[3] - 30
                              and len(re.findall(r'[\u3400-\u9fff]', line['text'])) >= 6
                              and not re.search(r'Reads|Likes|阅读|点赞|官方公众号|私信|关注', line['text'], re.I)
                              and _normalize(line['text']) not in visited_titles]
                for candidate in sorted(candidates, key=lambda row: (row['top'], row['left'])):
                    desktop.check_time()
                    title = _normalize(candidate['text'])
                    # Re-resolve after navigation: never reuse a stale screenshot coordinate.
                    fresh = desktop.lines()
                    matches = [r for r in fresh if _normalize(r['text']) == title and r['top'] >= content_top]
                    if not matches:
                        continue
                    visited_titles.add(title)
                    desktop.click(matches[0]['x'], matches[0]['y'])
                    url = desktop.copy_article_url()
                    desktop.key('ctrl+w')
                    desktop.phase = 'profile'
                    if url not in seen_urls:
                        seen_urls.add(url)
                        links.append({'url': url, 'published_date': None, 'disallowed': False})
                    if len(links) >= limit:
                        break
                if len(links) >= limit:
                    break
                desktop.guard()
                desktop.command('xdotool', 'mousemove', str(desktop.geometry[0] + desktop.geometry[2] // 2),
                                str(desktop.geometry[1] + desktop.geometry[3] - 80),
                                'click', '--repeat', '5', '--delay', '80', '5')
                content_top = 48
            if not links:
                raise _blocked('没有取得已授权公众号中的公开文章链接；未宣称采集成功。')
        except acquisition.AcquisitionError as error:
            halt_required = (desktop.manual_halt or error.code == 'source_verification_required'
                             or bool(getattr(error, 'verification_required', False)))
            if halt_required:
                error.verification_required = True
            if not links:
                raise
            message = '公开界面读取中止；已发现的链接保留，正文与归属仍待采集核验。 ' + str(error)
        finally:
            desktop.cleanup()
        snapshot = {'links': links, 'complete': False, 'message': message}
        if halt_required:
            snapshot['verification_required'] = True
        return snapshot
