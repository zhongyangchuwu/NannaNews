"""Bounded adaptive PNG rendering of a staged bulletin workspace."""

import os
import subprocess

from nannanews.publication import typst_command
from .bulletin import DEFAULT_PPI, MAX_PNG_BYTES

def compile_png_adaptive(typ_path, out_base, root, requested_ppi=DEFAULT_PPI, timeout=300):
    import glob as _glob
    import math as _math

    try:
        requested_ppi = int(requested_ppi)
    except (TypeError, ValueError):
        requested_ppi = DEFAULT_PPI
    R = max(40, min(600, requested_ppi))
    LOW, PROBE, PREC = 40, 250, 5
    tried = []
    cache = {}

    def _clean():
        single = out_base if out_base.endswith('.png') else out_base + '.png'
        try:
            if os.path.exists(single):
                os.remove(single)
        except Exception:
            pass
        for old in _glob.glob(out_base + '-*.png'):
            try:
                os.remove(old)
            except Exception:
                pass
        return single

    def _once(ppi):
        if ppi in cache:
            return cache[ppi]
        single = _clean()
        cmd = typst_command() + ['--root', str(root), '--input', 'issue=issue.json', '--pages', '1', '--ppi', str(int(ppi)), str(typ_path), single]
        try:
            r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            tried.append((ppi, 'timeout'))
            cache[ppi] = ('timeout', None, None)
            return cache[ppi]
        if r.returncode == 0 and os.path.exists(single):
            size = os.path.getsize(single)
            tried.append((ppi, size))
            cache[ppi] = ('ok', size, single)
            return cache[ppi]
        tried.append((ppi, 'compile-fail'))
        cache[ppi] = ('fail', None, None)
        return cache[ppi]

    def _fits(ppi):
        st, size, _ = _once(ppi)
        return st == 'ok' and size is not None and size <= MAX_PNG_BYTES

    def _size(ppi):
        st, size, _ = _once(ppi)
        return size if st == 'ok' else None

    def _round5(x):
        return int(round(x / PREC) * PREC)

    def _estimate(lo, s_lo, hi, s_hi):
        try:
            if s_lo is None or s_hi is None:
                return None
            if not (s_lo <= MAX_PNG_BYTES < s_hi):
                return None
            if s_hi <= s_lo:
                return None
            denom = hi * hi - lo * lo
            if denom <= 0:
                return None
            a = (s_hi - s_lo) / denom
            if a <= 0:
                return None
            b = s_lo - a * lo * lo
            if MAX_PNG_BYTES <= b:
                return None
            p_est = _math.sqrt((MAX_PNG_BYTES - b) / a)
            p_est = _round5(p_est)
            if p_est <= lo or p_est >= hi:
                return None
            return p_est
        except Exception:
            return None

    def _bisect(lo, s_lo, hi, s_hi):
        guard = 0
        while hi - lo > PREC and guard < 20:
            guard += 1
            est = _estimate(lo, s_lo, hi, s_hi)
            if est is None:
                mid = _round5((lo + hi) / 2)
            else:
                mid = est
            if mid <= lo:
                mid = lo + PREC
            elif mid >= hi:
                mid = hi - PREC
            mid = _round5(mid)
            if mid <= lo:
                mid = lo + PREC
            if mid >= hi:
                mid = hi - PREC
                mid = _round5(mid)
            if mid <= lo or mid >= hi:
                break
            st, size, _ = _once(mid)
            if st == 'ok' and size is not None and size <= MAX_PNG_BYTES:
                lo, s_lo = mid, size
            else:
                hi = mid
                if st == 'ok' and size is not None:
                    s_hi = size
        return lo

    if _fits(R):
        st, size, path = cache[R]
        single = out_base if out_base.endswith('.png') else out_base + '.png'
        if not os.path.exists(single) and not _glob.glob(out_base + '-*.png'):
            cache.pop(R, None)
            st, size, path = _once(R)
        return R, path, size, tried
    if R <= PROBE:
        if R == LOW:
            st, size, path = cache[R]
            return LOW, path, size, tried
        st_low, size_low, _ = _once(LOW)
        if st_low != 'ok':
            raise RuntimeError(f"PNG 编译失败：tried={tried}")
        if size_low is not None and size_low > MAX_PNG_BYTES:
            st, size, path = cache[LOW]
            return LOW, path, size, tried
        s_hi = _size(R)
        opt = _bisect(LOW, size_low, R, s_hi)
        cache.pop(opt, None)
        st, size, path = _once(opt)
        return opt, path, size, tried

    st_p, size_p, _ = _once(PROBE)
    if st_p != 'ok':
        st_low, size_low, _ = _once(LOW)
        if st_low != 'ok':
            raise RuntimeError(f"PNG 编译失败：tried={tried}")
        if size_low is not None and size_low > MAX_PNG_BYTES:
            st, size, path = cache[LOW]
            return LOW, path, size, tried
        opt = _bisect(LOW, size_low, PROBE, None)
        cache.pop(opt, None)
        st, size, path = _once(opt)
        return opt, path, size, tried
    if size_p is not None and size_p > MAX_PNG_BYTES:
        st_low, size_low, _ = _once(LOW)
        if st_low != 'ok':
            raise RuntimeError(f"PNG 编译失败：tried={tried}")
        if size_low is not None and size_low > MAX_PNG_BYTES:
            st, size, path = cache[LOW]
            return LOW, path, size, tried
        opt = _bisect(LOW, size_low, PROBE, size_p)
        cache.pop(opt, None)
        st, size, path = _once(opt)
        return opt, path, size, tried
    s_hi = _size(R)
    opt = _bisect(PROBE, size_p, R, s_hi)
    cache.pop(opt, None)
    st, size, path = _once(opt)
    return opt, path, size, tried


