"""Convert the reviewed workbook JSON into a public-only source directory.

CLI: nannanews import-source-catalog reviewed-workbooks.json output.json
No workbook contacts, group identifiers, private notes, or raw rows are copied.
"""
import ipaddress
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def public_urls(value):
    urls = []
    for candidate in re.findall(r'https?://[^\s；;，,<>"\)）]+', value or ''):
        try:
            parts = urlsplit(candidate)
            host = (parts.hostname or '').lower().rstrip('.')
            if (not host or parts.scheme not in {'http', 'https'} or parts.username is not None
                    or parts.password is not None or host == 'localhost'
                    or host.endswith(('.localhost', '.local', '.internal'))
                    or host in {'qm.qq.com', 'qun.qq.com', 'jq.qq.com'}):
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                pass
            urls.append(urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, '')))
        except ValueError:
            continue
    return list(dict.fromkeys(urls))


def build_catalog(workbooks):
    """Explicit public column maps, never arbitrary header/row serialization."""
    accounts = {}
    # Detailed source sheets precede flat comparison lists regardless of input order.
    priority = ('微信公众号名单', '社团清单', '部门和院系清单', '新的公众号清单', '公众号_两组差异')
    workbooks = sorted(workbooks, key=lambda book: next(
        (rank for rank, marker in enumerate(priority) if marker in book.get('file', '')), len(priority)))
    for workbook in workbooks:
        filename = workbook.get('file', '')
        for sheet in workbook.get('sheets', []):
            if sheet.get('state', 'visible') != 'visible':
                continue
            rows = sheet.get('rows', [])
            if not rows:
                continue
            # Contact, private-note and suggested-alias columns are never accessed.
            if sheet['name'] == '名单' and '微信公众号名单' in filename:
                mapping = {'name': 'D', 'category': 'B', 'subcategory': 'C', 'account_type': 'E', 'unit': 'G', 'wechat_id': 'J', 'description': 'M', 'source_urls': 'O'}
                start, link_columns = 2, ('O',)
            elif filename.startswith('南京大学公众号_两组差异') and sheet['name'] == '第一组独有及疑似项':
                mapping = {'name': 'B', 'category': 'H', 'subcategory': 'I', 'account_type': 'J', 'unit': 'L'}
                start, link_columns = 8, ()
            elif filename.startswith('南京大学公众号_两组差异') and sheet['name'] == '第二组名称与来源':
                mapping = {'name': 'A'}
                start, link_columns = 2, ()
            elif sheet['name'] == 'Sheet1' and filename.startswith('社团清单'):
                mapping = {'name': 'E', 'category': None, 'unit': 'A'}
                start, link_columns = 2, ('E',)
            elif sheet['name'] == 'Sheet1' and filename.startswith('部门和院系清单'):
                mapping = {'name': 'C', 'category': None, 'unit': 'A', 'source_urls': 'D'}
                start, link_columns = 2, ('C', 'D')
            elif sheet['name'] == 'Sheet1' and filename.startswith('新的公众号清单'):
                mapping = {'name': 'A', 'category': None, 'unit': 'D'}
                start, link_columns = 1, ('A',)
            else:
                continue
            for row in rows:
                if row.get('hidden') or row.get('row', 0) < start:
                    continue
                cells = row.get('cells', {})
                names = [name.strip() for name in re.split('[；;]', str(cells.get(mapping['name'], '')))]
                for name in names:
                    if not name or name in {'无', '/', '\\', '公众号', '公众号名称'}:
                        continue
                    key = re.sub(r'\s+', '', name).casefold()
                    fields = {field: str(cells.get(column, '')).strip() for field, column in mapping.items()
                              if column and field not in {'name', 'source_urls'}}
                    fields['name'] = name
                    fields['category'] = fields.get('category') or ('社团' if filename.startswith('社团清单') else '院系部门' if filename.startswith('部门和院系清单') else '其他公开账号')
                    urls = public_urls(str(cells.get(mapping.get('source_urls'), '')))
                    links = row.get('hyperlinks', {})
                    for column in link_columns:
                        for target in links.get(column, []):
                            urls.extend(public_urls(target))
                    fields['source_urls'] = list(dict.fromkeys(urls))
                    fields['id'] = hashlib.sha256(key.encode()).hexdigest()[:16]
                    fields['verification_state'] = 'historical_directory_only'
                    if fields.get('wechat_id') and not re.fullmatch(r'[A-Za-z0-9_-]{2,64}', fields['wechat_id']):
                        fields.pop('wechat_id')
                    provenance = {'workbook': filename, 'sha256': workbook.get('sha256'), 'sheet': sheet['name'], 'row': row['row']}
                    if key not in accounts:
                        fields['provenance'] = [provenance]
                        accounts[key] = fields
                    else:
                        account = accounts[key]
                        if provenance not in account['provenance']:
                            account['provenance'].append(provenance)
                        account['source_urls'] = list(dict.fromkeys(account['source_urls'] + fields['source_urls']))
                        for field, value in fields.items():
                            if field not in {'id', 'source_urls'} and value and not account.get(field):
                                account[field] = value
    return sorted(accounts.values(), key=lambda item: (item['category'], item['name']))


def import_catalog(input_path, output_path):
    """Write only public account fields from reviewed workbook JSON."""
    input_path, output_path = Path(input_path), Path(output_path)
    workbooks = json.loads(input_path.read_text(encoding='utf-8'))
    if not isinstance(workbooks, list):
        raise ValueError('input must be the reviewed workbook JSON array')
    catalog = build_catalog(workbooks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return len(catalog)
