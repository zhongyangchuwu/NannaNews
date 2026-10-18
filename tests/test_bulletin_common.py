"""Date claims must not become fabricated expiry decisions."""
from pathlib import Path

import pytest



from nannanews.bulletin_common import date_field


@pytest.mark.parametrize('raw, expected, resolution', [
    ('2026-10-13', '2026-10-13', 'explicit'),
    ('2026年10月13日 14:30', '2026-10-13', 'explicit'),
    ('2024.2.29', '2024-02-29', 'explicit'),
    ('2025-02-29', None, 'invalid'),
    ('2026-13-01', None, 'invalid'),
    ('10-14', None, 'year_unknown'),
    ('10月14日 14:00', None, 'year_unknown'),
    ('2026年10月14日—12月31日', None, 'ambiguous'),
    ('2026-10-14至2026-10-16', None, 'ambiguous'),
    ('2026年10月14日至16日', None, 'ambiguous'),
    ('2026-10-14-16', None, 'ambiguous'),
    ('2026-10-14 14:00-16:00', '2026-10-14', 'explicit'),
    ('未注明截止时间', None, 'ambiguous'),
])
def test_date_claims_require_one_valid_explicit_year(raw, expected, resolution):
    claim = date_field('deadline', raw)
    assert claim['normalized_date'] == expected
    assert claim['resolution'] == resolution
