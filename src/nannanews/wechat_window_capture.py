"""Guest-side XComposite capture of one verified public WeChat window.

Executed with the dedicated image's stdlib Python; no root/focused screenshot.
XComposite's off-screen storage contains the named window and its descendants,
not overlapping sibling windows. See X.Org Xcomposite(3).
"""
import ctypes as c
import os
import re
import struct
import subprocess
import sys
import time


_PUBLIC_EXE = '/opt/wechat/RadiumWMPF/runtime/WeChatAppEx'


class _XImage(c.Structure):
    # Prefix of Xlib's XImage; the library owns the full allocation and destructor.
    _fields_ = [('width', c.c_int), ('height', c.c_int), ('xoffset', c.c_int),
                ('format', c.c_int), ('data', c.c_void_p), ('byte_order', c.c_int),
                ('bitmap_unit', c.c_int), ('bitmap_bit_order', c.c_int),
                ('bitmap_pad', c.c_int), ('depth', c.c_int), ('bytes_per_line', c.c_int),
                ('bits_per_pixel', c.c_int), ('red_mask', c.c_ulong),
                ('green_mask', c.c_ulong), ('blue_mask', c.c_ulong)]


class _ClientData(c.Union):
    _fields_ = [('b', c.c_char * 20), ('s', c.c_short * 10), ('l', c.c_long * 5)]


class _ClientMessage(c.Structure):
    _fields_ = [('type', c.c_int), ('serial', c.c_ulong), ('send_event', c.c_int),
                ('display', c.c_void_p), ('window', c.c_ulong), ('message_type', c.c_ulong),
                ('format', c.c_int), ('data', _ClientData)]


class _SelectionEvent(c.Structure):
    _fields_ = [('type', c.c_int), ('serial', c.c_ulong), ('send_event', c.c_int),
                ('display', c.c_void_p), ('requestor', c.c_ulong), ('selection', c.c_ulong),
                ('target', c.c_ulong), ('property', c.c_ulong), ('time', c.c_ulong)]


class _XEvent(c.Union):
    _fields_ = [('client', _ClientMessage), ('selection', _SelectionEvent), ('padding', c.c_long * 24)]


class _XcbExtension(c.Structure):
    _fields_ = [('name', c.c_char_p), ('global_id', c.c_int)]


class _XcbRequest(c.Structure):
    _fields_ = [('count', c.c_size_t), ('extension', c.POINTER(_XcbExtension)),
                ('opcode', c.c_uint8), ('isvoid', c.c_uint8)]


class _IOVec(c.Structure):
    _fields_ = [('base', c.c_void_p), ('length', c.c_size_t)]


class _XcbExtensionReply(c.Structure):
    _fields_ = [('response_type', c.c_uint8), ('pad', c.c_uint8), ('sequence', c.c_uint16),
                ('length', c.c_uint32), ('present', c.c_uint8), ('opcode', c.c_uint8),
                ('first_event', c.c_uint8), ('first_error', c.c_uint8)]


_XRESOURCE = _XcbExtension(b'X-Resource', 0)


def _libraries():
    x = c.CDLL('libX11.so.6')
    composite = c.CDLL('libXcomposite.so.1')
    x.XOpenDisplay.argtypes, x.XOpenDisplay.restype = [c.c_char_p], c.c_void_p
    x.XCloseDisplay.argtypes = [c.c_void_p]
    x.XSync.argtypes = [c.c_void_p, c.c_int]
    x.XInternAtom.argtypes, x.XInternAtom.restype = [c.c_void_p, c.c_char_p, c.c_int], c.c_ulong
    x.XSendEvent.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_long, c.POINTER(_XEvent)]
    x.XDefaultRootWindow.argtypes, x.XDefaultRootWindow.restype = [c.c_void_p], c.c_ulong
    x.XCreateSimpleWindow.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_int, c.c_uint,
                                     c.c_uint, c.c_uint, c.c_ulong, c.c_ulong]
    x.XCreateSimpleWindow.restype = c.c_ulong
    x.XDestroyWindow.argtypes = [c.c_void_p, c.c_ulong]
    x.XGrabServer.argtypes = x.XUngrabServer.argtypes = [c.c_void_p]
    x.XGetSelectionOwner.argtypes, x.XGetSelectionOwner.restype = [c.c_void_p, c.c_ulong], c.c_ulong
    x.XSetSelectionOwner.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_ulong]
    x.XConvertSelection.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_ulong, c.c_ulong, c.c_ulong]
    x.XPending.argtypes = [c.c_void_p]
    x.XNextEvent.argtypes = [c.c_void_p, c.POINTER(_XEvent)]
    x.XGetWindowProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_long, c.c_long,
                                    c.c_int, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_int),
                                    c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                    c.POINTER(c.POINTER(c.c_ubyte))]
    x.XFree.argtypes = [c.c_void_p]
    x.XGetGeometry.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong),
                              c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_uint),
                              c.POINTER(c.c_uint), c.POINTER(c.c_uint), c.POINTER(c.c_uint)]
    x.XGetImage.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_int,
                           c.c_uint, c.c_uint, c.c_ulong, c.c_int]
    x.XGetImage.restype = c.POINTER(_XImage)
    x.XDestroyImage.argtypes = [c.POINTER(_XImage)]
    x.XFreePixmap.argtypes = [c.c_void_p, c.c_ulong]
    composite.XCompositeQueryVersion.argtypes = [c.c_void_p, c.POINTER(c.c_int), c.POINTER(c.c_int)]
    composite.XCompositeRedirectWindow.argtypes = [c.c_void_p, c.c_ulong, c.c_int]
    composite.XCompositeUnredirectWindow.argtypes = [c.c_void_p, c.c_ulong, c.c_int]
    composite.XCompositeNameWindowPixmap.argtypes = [c.c_void_p, c.c_ulong]
    composite.XCompositeNameWindowPixmap.restype = c.c_ulong
    return x, composite


def _capture(window):
    x, composite = _libraries()
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError('Display unavailable')
    image, pixmap, redirected = None, 0, False
    try:
        major, minor = c.c_int(), c.c_int()
        if not composite.XCompositeQueryVersion(display, c.byref(major), c.byref(minor)) or (major.value, minor.value) < (0, 2):
            raise ValueError('Composite unavailable')
        composite.XCompositeRedirectWindow(display, window, 0)
        redirected = True
        x.XSync(display, 0)
        pixmap = composite.XCompositeNameWindowPixmap(display, window)
        x.XSync(display, 0)
        root, left, top = c.c_ulong(), c.c_int(), c.c_int()
        width, height, border, depth = c.c_uint(), c.c_uint(), c.c_uint(), c.c_uint()
        if not x.XGetGeometry(display, pixmap, c.byref(root), c.byref(left), c.byref(top),
                              c.byref(width), c.byref(height), c.byref(border), c.byref(depth)):
            raise ValueError('Window storage unavailable')
        if not (0 < width.value <= 1440 and 0 < height.value <= 1000):
            raise ValueError('Window outside capture bound')
        image = x.XGetImage(display, pixmap, 0, 0, width, height, c.c_ulong(-1).value, 2)
        if not image:
            raise ValueError('Window image unavailable')
        value = image.contents
        if (value.width != width.value or value.height != height.value or not value.data
                or value.byte_order != 0 or value.bits_per_pixel != 32 or value.depth not in (24, 32)
                # XGetImage on a pixmap has no Visual and reports zero masks.
                # The dedicated 24-bit Xvfb/ARGB32 surfaces use little-endian BGRX.
                or (value.red_mask, value.green_mask, value.blue_mask) not in
                    ((0, 0, 0), (0xff0000, 0xff00, 0xff))
                or not value.width * 4 <= value.bytes_per_line <= value.width * 4 + 256):
            raise ValueError('Unsupported window pixel format')
        return (struct.pack('!III', value.width, value.height, value.bytes_per_line)
                + c.string_at(value.data, value.bytes_per_line * value.height))
    finally:
        if image:
            x.XDestroyImage(image)
        if pixmap:
            x.XFreePixmap(display, pixmap)
        if redirected:
            composite.XCompositeUnredirectWindow(display, window, 0)
        x.XCloseDisplay(display)


def _request_close(window):
    x, _ = _libraries()
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError('Display unavailable')
    try:
        event = _XEvent()
        event.client.type = 33  # ClientMessage, not XDestroyWindow.
        event.client.send_event = 1
        event.client.display = display
        event.client.window = window
        event.client.message_type = x.XInternAtom(display, b'WM_PROTOCOLS', 0)
        event.client.format = 32
        event.client.data.l[0] = x.XInternAtom(display, b'WM_DELETE_WINDOW', 0)
        if not x.XSendEvent(display, window, 0, 0, c.byref(event)):
            raise ValueError('Window close request refused')
        x.XSync(display, 0)
    finally:
        x.XCloseDisplay(display)


def _geometry(window):
    text = subprocess.check_output(['xdotool', 'getwindowgeometry', '--shell', window],
                                   stderr=subprocess.DEVNULL, timeout=5).decode()
    fields = dict(re.findall(r'^(X|Y|WIDTH|HEIGHT)=(-?\d+)$', text, re.M))
    if len(fields) != 4:
        raise ValueError('Window geometry unavailable')
    return tuple(int(fields[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT'))


def _verify_menu(parent, window, pid):
    if parent == window:
        raise ValueError('A distinct menu window is required')
    metadata = subprocess.check_output(['xprop', '-id', window, '_NET_WM_PID', '_NET_WM_WINDOW_TYPE'],
                                       stderr=subprocess.DEVNULL, timeout=5).decode()
    if (not re.search(r'_NET_WM_PID\(CARDINAL\) = ' + re.escape(pid) + r'\s*$', metadata, re.M)
            or not re.search(r'_NET_WM_WINDOW_TYPE\(ATOM\) = _NET_WM_WINDOW_TYPE_MENU\s*$', metadata, re.M)):
        raise ValueError('Menu owner rejected')
    left, top, width, height = _geometry(parent)
    x, y, w, h = _geometry(window)
    if not (0 < w <= width and 0 < h <= height and left <= x and top <= y
            and x + w <= left + width and y + h <= top + height):
        raise ValueError('Menu outside public window')


def _pid_reply(reply):
    """Decode one LocalClientPID value, never infer a PID from XID bit patterns."""
    if len(reply) != 48:
        return None
    response, _, _, length, count = struct.unpack_from('=BBHII', reply)
    _, mask, value_bytes, pid = struct.unpack_from('=IIII', reply, 32)
    if response != 1 or length != 4 or count != 1 or mask != 2 or value_bytes != 4 or not pid:
        return None
    return pid


def _owner_pid(x, display, owner):
    # Qt's hidden selection window lacks _NET_WM_PID. X-Resource QueryClientIds
    # reports its actual OS owner instead; no window property or private payload
    # is trusted. Wire layout: xcb-proto/src/res.xml, request opcode 4 / PID mask 2.
    bridge = c.CDLL('libX11-xcb.so.1')
    xcb = c.CDLL('libxcb.so.1')
    libc = c.CDLL(None)
    bridge.XGetXCBConnection.argtypes, bridge.XGetXCBConnection.restype = [c.c_void_p], c.c_void_p
    xcb.xcb_get_extension_data.argtypes = [c.c_void_p, c.POINTER(_XcbExtension)]
    xcb.xcb_get_extension_data.restype = c.POINTER(_XcbExtensionReply)
    xcb.xcb_send_request.argtypes = [c.c_void_p, c.c_int, c.POINTER(_IOVec), c.POINTER(_XcbRequest)]
    xcb.xcb_send_request.restype = c.c_uint
    xcb.xcb_wait_for_reply.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_void_p)]
    xcb.xcb_wait_for_reply.restype = c.c_void_p
    xcb.xcb_flush.argtypes, xcb.xcb_flush.restype = [c.c_void_p], c.c_int
    libc.free.argtypes, libc.free.restype = [c.c_void_p], None
    # Share the Xlib connection: ownership checks run while this client has the
    # server grabbed. A second XCB connection would deadlock behind that grab.
    x.XSync(display, 0)
    connection = bridge.XGetXCBConnection(display)
    if not connection:
        return None
    extension = xcb.xcb_get_extension_data(connection, c.byref(_XRESOURCE))
    if not extension or not extension.contents.present:
        return None
    payload = (c.c_uint8 * 16).from_buffer_copy(bytes(4) + struct.pack('=III', 1, owner, 2))
    vectors = (_IOVec * 4)()
    vectors[2] = _IOVec(c.cast(payload, c.c_void_p), 16)
    # XCB requires two valid preceding iovec slots for its private framing.
    request = _XcbRequest(2, c.pointer(_XRESOURCE), 4, 0)
    sequence = xcb.xcb_send_request(connection, 1,
        c.cast(c.byref(vectors, 2 * c.sizeof(_IOVec)), c.POINTER(_IOVec)), c.byref(request))
    if not sequence or xcb.xcb_flush(connection) <= 0:
        return None
    error = c.c_void_p()
    result = xcb.xcb_wait_for_reply(connection, sequence, c.byref(error))
    try:
        if not result or error:
            return None
        response, _, _, length, count = struct.unpack('=BBHII', c.string_at(result, 12))
        if response != 1 or length != 4 or count != 1:
            return None
        return _pid_reply(c.string_at(result, 48))
    finally:
        if result:
            libc.free(result)
        if error:
            libc.free(error)


def _selection_owner():
    x, _ = _libraries()
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError('Display unavailable')
    try:
        return x.XGetSelectionOwner(display, x.XInternAtom(display, b'CLIPBOARD', 0))
    finally:
        x.XCloseDisplay(display)


def _clear_selection(previous, pid):
    x, _ = _libraries()
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError('Display unavailable')
    try:
        atom = x.XInternAtom(display, b'CLIPBOARD', 0)
        x.XGrabServer(display)
        try:
            if (x.XGetSelectionOwner(display, atom) == previous
                    and _owner_pid(x, display, previous) == pid):
                x.XSetSelectionOwner(display, atom, 0, 0)
        finally:
            x.XUngrabServer(display)
            x.XSync(display, 0)
    finally:
        x.XCloseDisplay(display)


def _read_selection(pid, previous):
    x, _ = _libraries()
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError('Display unavailable')
    requestor = 0
    try:
        selection = x.XInternAtom(display, b'CLIPBOARD', 0)
        utf8 = x.XInternAtom(display, b'UTF8_STRING', 0)
        prop = x.XInternAtom(display, b'NANNANEWS_PUBLIC_COPY', 0)
        # Bind conversion to the checked owner in one server transaction. A
        # separate xclip process would otherwise race a private selection owner.
        x.XGrabServer(display)
        try:
            owner = x.XGetSelectionOwner(display, selection)
            if owner == previous:
                return b''
            if not owner or _owner_pid(x, display, owner) != pid:
                raise ValueError('Clipboard owner is not the public browser')
            requestor = x.XCreateSimpleWindow(display, x.XDefaultRootWindow(display), 0, 0, 1, 1, 0, 0, 0)
            x.XConvertSelection(display, selection, utf8, prop, requestor, 0)
        finally:
            x.XUngrabServer(display)
            x.XSync(display, 0)
        ready = time.monotonic() + 3
        while time.monotonic() < ready:
            if not x.XPending(display):
                time.sleep(0.01)
                continue
            event = _XEvent()
            x.XNextEvent(display, c.byref(event))
            notice = event.selection
            if (notice.type != 31 or notice.requestor != requestor
                    or notice.selection != selection or notice.target != utf8):
                continue
            if notice.property != prop:
                raise ValueError('Clipboard conversion unavailable')
            x.XGrabServer(display)
            data = c.POINTER(c.c_ubyte)()
            try:
                if (x.XGetSelectionOwner(display, selection) != owner
                        or _owner_pid(x, display, owner) != pid):
                    raise ValueError('Clipboard owner changed before reading')
                actual, form, count, after = c.c_ulong(), c.c_int(), c.c_ulong(), c.c_ulong()
                status = x.XGetWindowProperty(display, requestor, prop, 0, 2048, 1, utf8,
                                              c.byref(actual), c.byref(form), c.byref(count), c.byref(after), c.byref(data))
                if status or actual.value != utf8 or form.value != 8 or after.value or count.value > 8192 or not data:
                    raise ValueError('Clipboard URL format unavailable')
                result = c.string_at(data, count.value)
                if x.XGetSelectionOwner(display, selection) != owner:
                    raise ValueError('Clipboard owner changed while reading')
                return struct.pack('!I', owner) + result
            finally:
                if data:
                    x.XFree(data)
                x.XUngrabServer(display)
                x.XSync(display, 0)
        raise ValueError('Clipboard owner did not answer')
    finally:
        if requestor:
            x.XDestroyWindow(display, requestor)
        x.XCloseDisplay(display)


def main():
    arguments = sys.argv[1:]
    operation = arguments[0] if arguments and arguments[0].startswith('--') else ''
    count = {'--close': 2, '--owner': 2, '--clear': 3, '--clipboard': 3, '--menu': 3}.get(operation, 1)
    if len(arguments) != count or operation and operation not in {'--close', '--owner', '--clear', '--clipboard', '--menu'}:
        raise ValueError('Public window operation required')
    parent = arguments[1] if operation else arguments[0]
    window = arguments[2] if operation == '--menu' else parent
    values = (parent, arguments[2]) if count == 3 else (parent,)
    if any(not value.isdecimal() or not 0 < int(value) < 2**32 for value in values):
        raise ValueError('Window required')
    metadata = subprocess.check_output(['xprop', '-id', parent, 'WM_CLASS', '_NET_WM_PID'],
                                       stderr=subprocess.DEVNULL, timeout=5).decode()
    if not re.search(r'WM_CLASS\(STRING\) = "com\.tencent\.wechatappex", "WeChatAppEx"', metadata):
        raise ValueError('Window owner rejected')
    pid = re.search(r'_NET_WM_PID\(CARDINAL\) = (\d+)', metadata)
    if not pid or os.readlink('/proc/' + pid[1] + '/exe') != _PUBLIC_EXE:
        raise ValueError('Window executable rejected')
    if operation == '--menu':
        _verify_menu(parent, window, pid[1])
    if operation == '--close':
        _request_close(int(window))
    elif operation == '--owner':
        sys.stdout.write(str(_selection_owner()))
    elif operation == '--clear':
        _clear_selection(int(arguments[2]), int(pid[1]))
    elif operation == '--clipboard':
        sys.stdout.buffer.write(_read_selection(int(pid[1]), int(arguments[2])))
    else:
        sys.stdout.buffer.write(_capture(int(window)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Public window operation refused') from None
