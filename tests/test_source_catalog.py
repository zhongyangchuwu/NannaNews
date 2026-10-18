"""Public roster imports retain account boundaries without disclosing private cells."""
import json


from nannanews import source_catalog as importer


def test_comparison_accounts_keep_distinct_names_and_detailed_metadata():
    comparison = {'file': '南京大学公众号_两组差异.xlsx', 'sha256': 'comparison', 'sheets': [
        {'name': '第一组独有及疑似项', 'rows': [
            {'row': 3, 'cells': {'B': '比较说明，不是账号'}},
            {'row': 8, 'cells': {'B': '公开甲', 'D': '公开乙', 'E': 'private-comparison-note',
                                'K': 'private-owner', 'L': '公开单位'}},
        ]},
        {'name': '第二组名称与来源', 'rows': [
            {'row': 1, 'cells': {'A': '公众号名称'}},
            {'row': 2, 'cells': {'A': '公开乙', 'B': 'private-origin-note'}},
        ]},
    ]}
    detailed = {'file': '南京大学微信公众号名单_2026-07-21.xlsx', 'sha256': 'detailed', 'sheets': [
        {'name': '名单', 'rows': [
            {'row': 2, 'cells': {'D': '公开甲', 'B': '学生组织', 'G': '公开单位', 'J': 'public_account'}},
            {'row': 3, 'hidden': True, 'cells': {'D': 'private-hidden-account'}},
        ]},
    ]}
    catalog = importer.build_catalog([comparison, detailed])
    accounts = {item['name']: item for item in catalog}
    assert set(accounts) == {'公开甲', '公开乙'}
    assert accounts['公开甲']['category'] == '学生组织'
    assert accounts['公开甲']['wechat_id'] == 'public_account'
    assert {p['workbook'] for p in accounts['公开甲']['provenance']} == {
        comparison['file'], detailed['file']}
    serialized = json.dumps(catalog, ensure_ascii=False)
    for private in ('private-comparison-note', 'private-owner', 'private-origin-note', 'private-hidden-account'):
        assert private not in serialized


def test_semicolon_accounts_are_individual_sources_with_shared_row_provenance():
    workbook = {'file': '部门和院系清单.xlsx', 'sheets': [{'name': 'Sheet1', 'rows': [
        {'row': 2, 'cells': {'A': '公开学院', 'C': '公开甲；公开乙;公开甲', 'E': 'private-contact'}},
    ]}]}
    catalog = importer.build_catalog([workbook])
    assert {item['name'] for item in catalog} == {'公开甲', '公开乙'}
    assert len({item['id'] for item in catalog}) == 2
    for item in catalog:
        assert item['unit'] == '公开学院'
        assert item['provenance'] == [{'workbook': workbook['file'], 'sha256': None, 'sheet': 'Sheet1', 'row': 2}]
        assert 'private-contact' not in json.dumps(item)


def test_only_account_source_hyperlinks_are_exported():
    workbook = {'file': '社团清单.xlsx', 'sheets': [{'name': 'Sheet1', 'rows': [
        {'row': 2, 'cells': {'A': '公开社团', 'E': '公开账号', 'F': 'private-group', 'G': 'private-contact'},
         'hyperlinks': {'E': ['https://public.example.org/account'],
                        'F': ['https://private.example.org/group-token'],
                        'G': ['https://private.example.org/contact-token']}},
    ]}]}
    catalog = importer.build_catalog([workbook])
    assert catalog[0]['source_urls'] == ['https://public.example.org/account']
    assert 'private-' not in json.dumps(catalog)


def test_contact_and_local_network_links_are_not_public_sources():
    workbook = {'file': '南京大学微信公众号名单_2026-07-21.xlsx', 'sheets': [{'name': '名单', 'rows': [
        {'row': 2, 'cells': {'D': '公开账号', 'O': ' '.join([
            'https://public.example.org/article#section',
            'http://127.0.0.1/private', 'https://10.0.0.2/private', 'http://[::1]/private',
            'http://localhost/private', 'https://host.local/private',
            'https://user:private-password@public.example.org/article',
            'https://qm.qq.com/private-invite',
        ])}},
    ]}]}
    catalog = importer.build_catalog([workbook])
    assert catalog[0]['source_urls'] == ['https://public.example.org/article']
