"""Privacy and single-flight boundaries; no real client, display, or network."""
import json
import struct
import time

import pytest

from nannanews import acquisition
from nannanews import wechat_desktop as desktop


def _window():
    window = desktop._PublicDesktop()
    window.window = '123'
    window.geometry = (100, 50, 922, 892)
    return window


def test_focus_loss_never_captures_or_reads_clipboard(monkeypatch):
    commands = []

    def boundary(*args, **kwargs):
        commands.append(args)
        if args == ('xdotool', 'getwindowfocus'):
            return b'456\n'
        pytest.fail('The collector crossed the lost-focus privacy boundary.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        _window().screen()
    assert caught.value.code == 'source_blocked'
    assert not any(command[0] in ('scrot', desktop._XCLIP) for command in commands)


def test_matching_window_class_with_wrong_executable_cannot_receive_input(monkeypatch):
    input_events = []

    def boundary(*args, **kwargs):
        if args == ('xdotool', 'getwindowfocus'):
            return b'123\n'
        if args[0] == 'xprop':
            return b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
        if args[0] == 'readlink':
            return b'/opt/other-client/WeChatAppEx\n'
        input_events.append(args)
        pytest.fail('An untrusted executable received a desktop action.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    with pytest.raises(acquisition.AcquisitionError):
        _window().click(10, 10)
    assert not input_events


def test_resized_window_invalidates_previous_click_coordinates(monkeypatch):
    input_events = []

    def boundary(*args, **kwargs):
        if args == ('xdotool', 'getwindowfocus'):
            return b'123\n'
        if args[0] == 'xprop':
            return b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
        if args[0] == 'readlink':
            return (desktop._PUBLIC_EXE + '\n').encode()
        if args[:2] == ('xdotool', 'getwindowgeometry'):
            return b'X=101\nY=50\nWIDTH=922\nHEIGHT=892\n'
        input_events.append(args)
        pytest.fail('Stale coordinates reached the input transport.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    with pytest.raises(acquisition.AcquisitionError):
        _window().click(50, 100)
    assert not input_events


def test_expired_discovery_does_not_touch_the_desktop(monkeypatch):
    window = _window()
    window.end = time.monotonic() - 1
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('An expired operation touched the desktop.'))
    with pytest.raises(acquisition.AcquisitionError):
        window.click(10, 10)


def test_different_databases_still_share_one_desktop_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    with desktop._desktop_lock():
        with pytest.raises(acquisition.AcquisitionError) as caught:
            with desktop._desktop_lock():
                pytest.fail('Concurrent collectors acquired one dedicated desktop.')
        assert caught.value.code == 'source_blocked'
    with desktop._desktop_lock():
        pass


def _source():
    return {'kind': 'wechat_gui', 'expected_identity': '南京大学', 'wechat_biz': 'MzAxODAzMjQ1NQ==',
            'url': 'https://mp.weixin.qq.com/s/reference',
            'identity_url': 'https://mp.weixin.qq.com/s/reference'}


def test_rejected_runtime_never_receives_cleanup_or_clipboard_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(desktop, '_command', lambda *a, **k: b'{} true 10001:10001')
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('A rejected runtime received docker exec.'))
    with pytest.raises(acquisition.AcquisitionError):
        desktop.discover_articles(_source(), page_limit=1)


def test_expired_bootstrap_never_inspects_or_activates_a_window(monkeypatch):
    window = _window()
    window.end = time.monotonic() - 1
    monkeypatch.setattr(desktop, '_command', lambda *a, **k: pytest.fail('Expired bootstrap accessed the runtime.'))
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('Expired bootstrap accessed the display.'))
    with pytest.raises(acquisition.AcquisitionError):
        window.open_search()


def test_focus_race_cannot_replace_the_explicit_public_capture_with_private_pixels(monkeypatch):
    window = _window()
    width, height = window.geometry[2:]
    public = bytes([0, 0, 255, 0]) * (width * height)
    private = bytes([255, 0, 0, 0]) * (width * height)

    def boundary(*args, **kwargs):
        if args == ('xdotool', 'getwindowfocus'):
            return b'123\n'
        if args[0] == 'xprop':
            return b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
        if args[0] == 'readlink':
            return (desktop._PUBLIC_EXE + '\n').encode()
        if args[:2] == ('xdotool', 'getwindowgeometry'):
            return b'X=100\nY=50\nWIDTH=922\nHEIGHT=892\n'
        if args[:2] == ('python3', '-c'):
            # Focus changes to private window 456 and back during capture. The
            # transport still has to request window 123, not the focused window.
            pixels = public if args[-1] == '123' else private
            return struct.pack('!III', width, height, width * 4) + pixels
        pytest.fail('Capture was not bound to the explicit public window.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    image = window.screen()
    assert image.getpixel((0, 0)) == image.getpixel((width - 1, height - 1)) == (255, 0, 0)


def test_bootstrap_geometry_change_never_clicks_the_old_native_location(monkeypatch):
    window = desktop._PublicDesktop()
    moved = False
    actions = []
    labels = {'com.docker.compose.project': 'nannanews-wechat-desktop', 'com.docker.compose.service': 'desktop'}
    monkeypatch.setattr(desktop, '_command', lambda *a, **k: (json.dumps(labels) + ' true 10001:10001 running').encode())

    def boundary(*args, **kwargs):
        nonlocal moved
        if args[0] == 'cat':
            return b'72\n'
        if args[0] == 'readlink':
            return b'/opt/wechat/wechat\n'
        if args[:2] == ('xdotool', 'search'):
            return b'111\n'
        if args[:2] == ('xdotool', 'getwindowgeometry'):
            return f'X={180 if moved else 100}\nY=50\nWIDTH=916\nHEIGHT=666\n'.encode()
        if args[:2] == ('xdotool', 'windowactivate'):
            moved = True
            return b''
        if args[0] == 'xprop':
            return b'_NET_WM_PID(CARDINAL) = 72\n'
        actions.append(args)
        pytest.fail('A stale native location reached an input action.')

    def accessibility(bus, path, method, *arguments):
        if method == 'org.a11y.Bus.GetAddress':
            return "('unix:path=/fixture/bus',)"
        if bus == 'org.a11y.atspi.Registry':
            return "([(':1.0', objectpath '/org/a11y/atspi/accessible/root')],)"
        if method.endswith('Properties.Get'):
            return "(<'wechat'>,)" if path.endswith('/root') else "(<'Search'>,)"
        if method.endswith('GetChildren'):
            child = 'frame' if path.endswith('/root') else 'search'
            return "([(':1.0', objectpath '/org/a11y/atspi/accessible/" + child + "')],)"
        if method.endswith('GetExtents'):
            return '((0, 0, 880, 640),)' if path.endswith('/frame') else '((1, 330, 60, 36),)'
        pytest.fail('Unexpected accessibility operation.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    monkeypatch.setattr(window, 'a11y', accessibility)
    with pytest.raises(acquisition.AcquisitionError):
        window.open_search()
    assert moved
    assert not actions


def test_discovered_links_survive_later_gui_and_cleanup_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    titles = [{'text': '南京大学第一篇公开活动通知', 'left': 200, 'top': 200, 'x': 350, 'y': 210},
              {'text': '南京大学第二篇公开活动通知', 'left': 200, 'top': 300, 'x': 350, 'y': 310}]
    urls = iter(['https://mp.weixin.qq.com/s/first', 'https://mp.weixin.qq.com/s/second'])

    def open_search(self):
        self.authorized = self.browser_owned = self.clipboard_owned = True
        self.window, self.geometry = '123', (100, 50, 922, 892)

    def boundary(*args, **kwargs):
        if args[:2] == ('xdotool', 'key'):
            return b''
        raise acquisition.AcquisitionError('专用客户端已退出。', 'source_blocked')

    monkeypatch.setattr(desktop._PublicDesktop, 'open_search', open_search)
    monkeypatch.setattr(desktop._PublicDesktop, 'search_account', lambda self, name: 100)
    monkeypatch.setattr(desktop._PublicDesktop, 'lines', lambda self: titles)
    monkeypatch.setattr(desktop._PublicDesktop, 'click', lambda *a: None)
    monkeypatch.setattr(desktop._PublicDesktop, 'guard', lambda self: None)
    monkeypatch.setattr(desktop._PublicDesktop, 'copy_article_url', lambda self: next(urls))
    monkeypatch.setattr(desktop, '_exec', boundary)
    result = desktop.discover_articles(_source(), page_limit=1)
    assert result['complete'] is False
    assert result['links'] == [
        {'url': 'https://mp.weixin.qq.com/s/first', 'published_date': None, 'disallowed': False},
        {'url': 'https://mp.weixin.qq.com/s/second', 'published_date': None, 'disallowed': False}]


@pytest.mark.parametrize('retained', [0, 1])
@pytest.mark.parametrize('code', ['source_verification_required', 'source_blocked'])
def test_manual_halt_preserves_discovered_urls_without_losing_stop_signal(tmp_path, monkeypatch, retained, code):
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    titles = [{'text': '南京大学第一篇公开通知', 'left': 200, 'top': 200, 'x': 350, 'y': 210},
              {'text': '南京大学第二篇公开通知', 'left': 200, 'top': 300, 'x': 350, 'y': 310}]
    copies = []

    def copy_article_url(window):
        if len(copies) < retained:
            copies.append('https://mp.weixin.qq.com/s/first')
            return copies[-1]
        window.manual_halt = True
        raise acquisition.AcquisitionError('公开操作需要人工处理。', code)

    monkeypatch.setattr(desktop._PublicDesktop, 'open_search',
                        lambda window: setattr(window, 'geometry', (100, 50, 922, 892)))
    monkeypatch.setattr(desktop._PublicDesktop, 'search_account', lambda window, name: 100)
    monkeypatch.setattr(desktop._PublicDesktop, 'lines', lambda window: titles)
    monkeypatch.setattr(desktop._PublicDesktop, 'click', lambda *args: None)
    monkeypatch.setattr(desktop._PublicDesktop, 'key', lambda *args: None)
    monkeypatch.setattr(desktop._PublicDesktop, 'copy_article_url', copy_article_url)
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('A manual halt touched the desktop.'))
    if retained:
        result = desktop.discover_articles(_source(), page_limit=1)
        assert result['links'] == [{'url': copies[0], 'published_date': None, 'disallowed': False}]
        assert result['complete'] is False and result['verification_required'] is True
    else:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            desktop.discover_articles(_source(), page_limit=1)
        assert caught.value.code == code and caught.value.verification_required is True



def test_account_search_opens_official_account_not_same_name_channel(monkeypatch):
    from PIL import Image

    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = _window()
    state = {'filtered': False, 'opened': None}
    header = 'block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    # Recorded public results: the official account name shares its OCR line
    # with a badge, while the same-name video channel has a clean name line.
    official = header
    for group, text, left, top, width, height in [
            (1, 'Official', 421, 348, 95, 22), (1, 'Accounts', 526, 350, 127, 20),
            (2, '南大', 251, 492, 70, 36), (2, '青年', 335, 492, 59, 36),
            (2, '单位', 460, 496, 38, 24)]:
        official += f'{group}\t1\t1\t{left}\t{top}\t{width}\t{height}\t99\t{text}\n'
    mixed = official + '3\t1\t1\t251\t1296\t70\t36\t99\t南大\n'
    mixed += '3\t1\t1\t332\t1296\t62\t36\t99\t青年\n'

    def click(x, y):
        if window.phase == 'account_results':
            if 210.5 <= x <= 326.5 and 174 <= y <= 185:
                state['filtered'] = True
            elif 125.5 <= x <= 197 and 246 <= y <= 264:
                state['opened'] = 'official' if state['filtered'] else 'unfiltered'
            elif 125.5 <= x <= 197 and 648 <= y <= 666:
                state['opened'] = 'channel'

    monkeypatch.setattr(window, 'click', click)
    monkeypatch.setattr(window, 'clipboard', lambda text: None)
    monkeypatch.setattr(window, 'key', lambda key: None)
    monkeypatch.setattr(desktop, '_command', lambda *args, **kwargs:
                        (official if state['filtered'] else mixed).encode())

    def lines():
        clock[0] += 1
        if window.phase == 'account_results':
            return window._recognize(Image.new('RGB', (922, 892)))
        if window.phase == 'profile':
            if state['opened'] != 'official':
                return []
            return [{'text': '南大青年', 'top': 69},
                    {'text': '文章', 'top': 275, 'bottom': 289, 'left': 369.5, 'x': 382.75, 'y': 282}]
        text, top = {'search_home': ('OfficialAccounts', 150),
                     'search_results': ('Accounts', 100)}[window.phase]
        return [{'text': text, 'top': top, 'left': 200, 'x': 250, 'y': top + 10}]

    monkeypatch.setattr(window, 'lines', lines)
    assert window.search_account('南大青年') == 301
    assert state['opened'] == 'official'


def test_account_search_stops_without_opening_ambiguous_official_accounts(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = _window()
    opened = []
    monkeypatch.setattr(window, 'clipboard', lambda text: None)
    monkeypatch.setattr(window, 'key', lambda key: None)
    monkeypatch.setattr(window, 'click', lambda x, y: opened.append((x, y)) if y > 240 else None)

    def lines():
        clock[0] += 1
        if window.phase == 'account_results':
            return [{'text': 'OfficialAccounts', 'top': 174, 'left': 210, 'x': 268, 'y': 179},
                    {'text': '南大青年', 'top': 246, 'left': 125, 'x': 161, 'y': 255},
                    {'text': '南大青年', 'top': 446, 'left': 125, 'x': 161, 'y': 455}]
        text, top = {'search_home': ('OfficialAccounts', 150),
                     'search_results': ('Accounts', 100)}[window.phase]
        return [{'text': text, 'top': top, 'left': 200, 'x': 250, 'y': top + 10}]

    monkeypatch.setattr(window, 'lines', lines)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        window.search_account('南大青年')
    assert caught.value.code == 'source_blocked'
    assert not opened


def test_account_name_span_does_not_hide_whole_line_verification_prompt(monkeypatch):
    from PIL import Image

    window = _window()
    window.phase = 'account_results'
    window.account_name = '南大青年'
    tsv = 'block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    for index, text in enumerate(['请完成', '南大', '青年', '验证']):
        tsv += f'1\t1\t1\t{200 + index * 80}\t492\t70\t36\t99\t{text}\n'
    monkeypatch.setattr(desktop, '_command', lambda *args, **kwargs: tsv.encode())
    with pytest.raises(acquisition.AcquisitionError) as caught:
        window._recognize(Image.new('RGB', (922, 892)))
    assert caught.value.code == 'source_verification_required'
    assert window.manual_halt is True


@pytest.mark.parametrize('article_label', ['文章', '“文章'])
def test_profile_uses_article_word_bounds_when_neighbor_tabs_share_ocr_line(monkeypatch, article_label):
    from PIL import Image

    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = _window()
    clicks = []
    header = 'block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    # Public 南大青年 profile: 全部 / 贴图 / 文章 were merged into one line.
    tsv = header + '1\t1\t1\t680\t138\t140\t36\t99\t南大青年\n'
    for text, left, top, width, height in [
            ('全', 530, 550, 56, 28), ('部', 570, 546, 32, 45),
            ('A', 636, 552, 52, 25), (article_label, 739, 550, 53, 28)]:
        tsv += f'16\t1\t1\t{left}\t{top}\t{width}\t{height}\t99\t{text}\n'
    monkeypatch.setattr(desktop, '_command', lambda *args, **kwargs: tsv.encode())
    monkeypatch.setattr(window, 'click', lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(window, 'clipboard', lambda text: None)
    monkeypatch.setattr(window, 'key', lambda key: None)

    def lines():
        clock[0] += 1
        if window.phase == 'profile':
            return window._recognize(Image.new('RGB', (922, 892)))
        if window.phase == 'account_results':
            return [{'text': 'OfficialAccounts', 'top': 174, 'left': 210, 'x': 268, 'y': 179},
                    {'text': '南大青年', 'top': 246, 'left': 125, 'x': 161, 'y': 255}]
        text, top = {'search_home': ('OfficialAccounts', 150),
                     'search_results': ('Accounts', 100)}[window.phase]
        return [{'text': text, 'top': top, 'left': 200, 'x': 250, 'y': top + 10}]

    monkeypatch.setattr(window, 'lines', lines)
    assert window.search_account('南大青年') == 301
    assert clicks[-1] == (382.75, 282.0)

def test_slow_public_page_waits_for_readiness_not_a_fixed_ocr_frame_count(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = desktop._PublicDesktop()

    def public_page():
        clock[0] += 5
        return [{'text': 'Accounts'}] if clock[0] >= 35 else []

    monkeypatch.setattr(window, 'lines', public_page)
    assert window.wait_lines(lambda rows: any(row['text'] == 'Accounts' for row in rows)) == [
        {'text': 'Accounts'}]


def test_public_page_wait_never_outlives_the_discovery_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = desktop._PublicDesktop()
    window.end = 20

    def public_page():
        clock[0] += 5
        return [{'text': 'Accounts'}] if clock[0] >= 35 else []

    monkeypatch.setattr(window, 'lines', public_page)
    with pytest.raises(acquisition.AcquisitionError):
        window.wait_lines(lambda rows: any(row['text'] == 'Accounts' for row in rows))
    assert clock[0] <= window.end


def test_cleanup_requests_client_exit_without_destroying_its_window(monkeypatch):
    from types import SimpleNamespace
    from nannanews import wechat_window_capture as capture

    state = {'window_alive': True, 'close_requested': False}
    metadata = (b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n'
                b'_NET_WM_PID(CARDINAL) = 72\n')

    def receive(display, window, propagate, mask, pointer):
        event = capture.c.cast(pointer, capture.c.POINTER(capture._XEvent)).contents.client
        state['close_requested'] = (event.type == 33 and event.window == 123
                                    and event.message_type == 101 and event.format == 32
                                    and event.data.l[0] == 102)
        return 1

    x = SimpleNamespace(XOpenDisplay=lambda name: 1, XCloseDisplay=lambda display: 0,
                        XSync=lambda *args: 0, XSendEvent=receive,
                        XInternAtom=lambda display, name, only: 101 if name == b'WM_PROTOCOLS' else 102)
    monkeypatch.setattr(capture, '_libraries', lambda: (x, None))
    monkeypatch.setattr(capture, 'os', SimpleNamespace(readlink=lambda path: capture._PUBLIC_EXE))
    monkeypatch.setattr(capture, 'subprocess', SimpleNamespace(
        check_output=lambda *a, **k: metadata, DEVNULL=None))
    monkeypatch.setattr(capture, 'sys', SimpleNamespace(argv=['capture', '--close', '123']))

    def boundary(*args, **kwargs):
        if args == ('xdotool', 'getwindowfocus'):
            return b'123\n'
        if args[0] == 'xprop':
            return metadata
        if args[0] == 'readlink':
            return (desktop._PUBLIC_EXE + '\n').encode()
        if args[:2] == ('xdotool', 'getwindowgeometry'):
            return b'X=100\nY=50\nWIDTH=922\nHEIGHT=892\n'
        if args[:2] == ('xdotool', 'windowclose'):
            state['window_alive'] = False
            return b''
        if args[:2] == ('python3', '-c') and args[-2:] == ('--close', '123'):
            capture.main()
            return b''
        pytest.fail('Unexpected desktop operation during public-browser shutdown.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    window = _window()
    window.authorized = window.browser_owned = True
    window.cleanup()
    assert state == {'window_alive': True, 'close_requested': True}


def test_manual_verification_halts_before_menu_or_clipboard_input(monkeypatch):
    from PIL import Image

    window = _window()
    window.authorized = window.browser_owned = True
    monkeypatch.setattr(window, 'screen', lambda: Image.new('RGB', (10, 10)))
    header = 'block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    monkeypatch.setattr(desktop, '_command', lambda *a, **k: (
        header + '1\t1\t1\t0\t0\t10\t10\t99\t环境异常\n').encode())
    monkeypatch.setattr(window, 'click', lambda *a: pytest.fail('A challenge received menu input.'))
    monkeypatch.setattr(window, 'clipboard', lambda *a: pytest.fail('A challenge changed the clipboard.'))
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('A challenge received close or clipboard input.'))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        window.copy_article_url()
    assert caught.value.code == 'source_verification_required'
    assert window.manual_halt
    window.cleanup()


def test_copy_link_uses_remaining_budget_and_does_not_require_ocr_publication_date(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(desktop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(desktop.time, 'sleep', lambda seconds: None)
    window = _window()
    window.authorized = True
    copied = [False]
    url = 'https://mp.weixin.qq.com/s/public-copy'

    def root_page():
        clock[0] += 0.1
        return []  # A gray publication label must not gate a genuine copied URL.

    def menu():
        clock[0] += 10
        return ([{'text': 'CopyLink', 'top': 80, 'left': 500, 'x': 520, 'y': 90}]
                if clock[0] >= 60 else [])

    def click(x, y):
        if (x, y) == (520, 90):
            copied[0] = True

    def boundary(*args, **kwargs):
        if '--owner' in args:
            return b'11'
        if '--clipboard' in args:
            assert copied[0]
            return struct.pack('!I', 12) + url.encode()
        return b''

    monkeypatch.setattr(window, 'lines', root_page)
    monkeypatch.setattr(window, 'menu_lines', menu)
    monkeypatch.setattr(window, 'click', click)
    monkeypatch.setattr(window, 'guard', lambda: None)
    monkeypatch.setattr(desktop, '_exec', boundary)
    assert window.copy_article_url() == url
    assert 60 <= clock[0] < 180


@pytest.mark.parametrize('focus,halt,expected_closes', [('123', False, 1), ('456', False, 0), ('123', True, 0)])
def test_expired_discovery_has_independent_safe_cleanup_budget(monkeypatch, focus, halt, expected_closes):
    window = _window()
    window.authorized = window.browser_owned = True
    window.manual_halt = halt
    window.end = time.monotonic() - 1
    previous = window.end
    closes = []

    def boundary(*args, **kwargs):
        if args == ('xdotool', 'getwindowfocus'):
            return (focus + '\n').encode()
        if args[0] == 'xprop':
            return b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
        if args[0] == 'readlink':
            return (desktop._PUBLIC_EXE + '\n').encode()
        if args[:2] == ('xdotool', 'getwindowgeometry'):
            return b'X=100\nY=50\nWIDTH=922\nHEIGHT=892\n'
        if '--close' in args:
            closes.append(args[-1])
            return b''
        pytest.fail('Unexpected expired-cleanup operation.')

    monkeypatch.setattr(desktop, '_exec', boundary)
    window.cleanup()
    assert len(closes) == expected_closes
    assert window.end == previous


@pytest.mark.parametrize('state', ['created', 'exited', 'paused', 'restarting'])
def test_nonrunning_dedicated_runtime_is_refused_before_account_access(tmp_path, monkeypatch, state):
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    labels = {'com.docker.compose.project': 'nannanews-wechat-desktop', 'com.docker.compose.service': 'desktop'}

    def inspect(arguments, **kwargs):
        # Docker only emits fields requested by the client. Omitting lifecycle
        # state must not let a stopped, otherwise authorized container through.
        suffix = ' ' + state if '.State.Status' in arguments[3] else ''
        return (json.dumps(labels) + ' true 10001:10001' + suffix).encode()

    monkeypatch.setattr(desktop, '_command', inspect)
    monkeypatch.setattr(desktop, '_exec', lambda *a, **k: pytest.fail('A nonrunning client received an account command.'))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        desktop.discover_articles(_source(), page_limit=1)
    assert caught.value.code == 'source_runtime_unavailable'
