"""Only an owned, contained menu may extend a public window's capture scope."""
import io
from types import SimpleNamespace

import pytest

from nannanews import wechat_window_capture as capture


@pytest.mark.parametrize('violation', [
    'private_pid', 'not_menu', 'mixed_window_type', 'outside_left', 'outside_bottom',
    'same_window', 'wrong_parent_class', 'wrong_executable',
])
def test_menu_capture_rejects_scope_violations_before_reading_pixels(monkeypatch, violation):
    captures = []
    child = '10' if violation == 'same_window' else '11'
    parent_class = 'other' if violation == 'wrong_parent_class' else 'com.tencent.wechatappex'
    parent = f'WM_CLASS(STRING) = "{parent_class}", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
    pid = 42 if violation == 'private_pid' else 72
    kind = '_NET_WM_WINDOW_TYPE_NORMAL' if violation == 'not_menu' else '_NET_WM_WINDOW_TYPE_MENU'
    if violation == 'mixed_window_type':
        kind += ', _NET_WM_WINDOW_TYPE_NORMAL'
    menu = f'_NET_WM_PID(CARDINAL) = {pid}\n_NET_WM_WINDOW_TYPE(ATOM) = {kind}\n'
    x = 90 if violation == 'outside_left' else 500
    y = 800 if violation == 'outside_bottom' else 80

    def metadata(arguments, **kwargs):
        if arguments[0] == 'xprop':
            return (parent if arguments[2] == '10' else menu).encode()
        if arguments[0] == 'xdotool':
            return (b'X=100\nY=50\nWIDTH=922\nHEIGHT=892\n' if arguments[-1] == '10'
                    else f'X={x}\nY={y}\nWIDTH=300\nHEIGHT=300\n'.encode())
        pytest.fail('Unexpected guest command.')

    monkeypatch.setattr(capture, 'subprocess', SimpleNamespace(check_output=metadata, DEVNULL=None))
    monkeypatch.setattr(capture, 'os', SimpleNamespace(
        readlink=lambda path: '/opt/other/app' if violation == 'wrong_executable' else capture._PUBLIC_EXE))
    monkeypatch.setattr(capture, 'sys', SimpleNamespace(argv=['capture', '--menu', '10', child],
                                                      stdout=SimpleNamespace(buffer=io.BytesIO())))
    monkeypatch.setattr(capture, '_capture', lambda window: captures.append(window) or b'')
    with pytest.raises(ValueError):
        capture.main()
    assert captures == []


def test_same_process_classless_menu_is_captured_without_the_parent_or_desktop(monkeypatch):
    captures = []
    root = b'WM_CLASS(STRING) = "com.tencent.wechatappex", "WeChatAppEx"\n_NET_WM_PID(CARDINAL) = 72\n'
    menu = b'_NET_WM_PID(CARDINAL) = 72\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_MENU\n'

    def metadata(arguments, **kwargs):
        if arguments[0] == 'xprop':
            return root if arguments[2] == '10' else menu
        return (b'X=100\nY=50\nWIDTH=922\nHEIGHT=892\n' if arguments[-1] == '10'
                else b'X=500\nY=80\nWIDTH=300\nHEIGHT=300\n')

    monkeypatch.setattr(capture, 'subprocess', SimpleNamespace(check_output=metadata, DEVNULL=None))
    monkeypatch.setattr(capture, 'os', SimpleNamespace(readlink=lambda path: capture._PUBLIC_EXE))
    monkeypatch.setattr(capture, 'sys', SimpleNamespace(argv=['capture', '--menu', '10', '11'],
                                                      stdout=SimpleNamespace(buffer=io.BytesIO())))
    monkeypatch.setattr(capture, '_capture', lambda window: captures.append(window) or b'')
    capture.main()
    assert captures == [11]


@pytest.mark.parametrize('owner_pid', [None, 42])
def test_unknown_or_private_clipboard_owner_is_rejected_before_conversion(monkeypatch, owner_pid):
    calls = []
    x = SimpleNamespace(XOpenDisplay=lambda name: 1, XCloseDisplay=lambda display: None,
                        XInternAtom=lambda *a: 6, XGrabServer=lambda *a: None,
                        XUngrabServer=lambda *a: None, XSync=lambda *a: None,
                        XGetSelectionOwner=lambda *a: 100,
                        XCreateSimpleWindow=lambda *a: calls.append('requestor'),
                        XConvertSelection=lambda *a: calls.append('conversion'))
    monkeypatch.setattr(capture, '_libraries', lambda: (x, None))
    monkeypatch.setattr(capture, '_owner_pid', lambda *a: owner_pid)
    with pytest.raises(ValueError):
        capture._read_selection(72, 99)
    assert calls == []


def test_clipboard_replacement_after_conversion_is_rejected_before_payload_read(monkeypatch):
    owners = iter([100, 101])
    calls = []

    def notify(display, pointer):
        event = capture.c.cast(pointer, capture.c.POINTER(capture._XEvent)).contents.selection
        event.type, event.requestor, event.selection, event.target, event.property = 31, 300, 6, 6, 6

    x = SimpleNamespace(XOpenDisplay=lambda name: 1, XCloseDisplay=lambda display: None,
                        XInternAtom=lambda *a: 6, XGrabServer=lambda *a: None,
                        XUngrabServer=lambda *a: None, XSync=lambda *a: None,
                        XGetSelectionOwner=lambda *a: next(owners), XDefaultRootWindow=lambda *a: 1,
                        XCreateSimpleWindow=lambda *a: 300, XDestroyWindow=lambda *a: None,
                        XConvertSelection=lambda *a: calls.append('converted'), XPending=lambda *a: 1,
                        XNextEvent=notify, XGetWindowProperty=lambda *a: calls.append('payload'))
    monkeypatch.setattr(capture, '_libraries', lambda: (x, None))
    monkeypatch.setattr(capture, '_owner_pid', lambda *a: 72)
    with pytest.raises(ValueError):
        capture._read_selection(72, 99)
    assert calls == ['converted']


def test_cleanup_does_not_clear_another_clipboard_owner(monkeypatch):
    cleared = []
    x = SimpleNamespace(XOpenDisplay=lambda name: 1, XCloseDisplay=lambda display: None,
                        XInternAtom=lambda *a: 6, XGrabServer=lambda *a: None,
                        XUngrabServer=lambda *a: None, XSync=lambda *a: None,
                        XGetSelectionOwner=lambda *a: 100,
                        XSetSelectionOwner=lambda *a: cleared.append(True))
    monkeypatch.setattr(capture, '_libraries', lambda: (x, None))
    monkeypatch.setattr(capture, '_owner_pid', lambda *a: 42)
    capture._clear_selection(100, 72)
    assert cleared == []


def _pid_packet(**overrides):
    import struct

    values = {'response': 1, 'length': 4, 'count': 1, 'client': 0x1200000,
              'mask': 2, 'value_bytes': 4, 'pid': 119}
    values.update(overrides)
    reply = bytearray(48)
    struct.pack_into('=BBHII', reply, 0, values['response'], 0, 1, values['length'], values['count'])
    struct.pack_into('=IIII', reply, 32, values['client'], values['mask'], values['value_bytes'], values['pid'])
    return bytes(reply)


def test_server_local_client_pid_reply_does_not_require_window_pid_property():
    assert capture._pid_reply(_pid_packet()) == 119


@pytest.mark.parametrize('changes', [
    {'response': 0}, {'length': 0}, {'length': 8}, {'count': 0}, {'count': 2},
    {'mask': 1}, {'mask': 3}, {'value_bytes': 8}, {'pid': 0},
])
def test_non_pid_or_ambiguous_server_reply_cannot_authorize_clipboard(changes):
    assert capture._pid_reply(_pid_packet(**changes)) is None


def test_partial_or_extra_pid_reply_is_rejected():
    reply = _pid_packet()
    assert capture._pid_reply(reply[:-1]) is None
    assert capture._pid_reply(reply + b'\0') is None
