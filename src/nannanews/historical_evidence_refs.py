"""Explicit historical-only transport; canonical V5 admission remains separate.

build_evidence_catalog(occurrence, sources) returns a JSON catalog with version,
scope and entries. Each entry has id, origin, url (null for bulletin fields),
start/end (Python character offsets), sha256 and exact text. All eligible fields
are covered, including their final character. Chunks are <=4000 characters with
512-character overlap, preferring a newline in the last 512 characters. There
are no decoded/rendered variants, metadata quotes, or per-character entries.

decode_evidence_response(response, occurrence, *, sources, mismatched=False)
rebuilds the trusted catalog and returns the canonical seven-key V5 response.
The reference response adds evidence_scope. Common quotes are {ref} or genuine
image {text,image_index}; field/college evidence adds url to either shape.
Claims replace raw_text with quote:{ref} (primary or bulletin) or genuine image
quote:{url,text,image_index}. Only explicit selections are resolved. No literal
text citation fallback or retained-response repair is provided. Callers MUST
then pass the result through decision_from_response and retain the ORIGINAL
reference response as the paid raw output. Fully resolved canonical JSON must
also fit acquisition.MAX_MODEL_BODY (2 MiB), preserving the unchanged receipt
storage preflight's expansion assumption; exceeding it fails, never truncates.
"""
import hashlib
import json

from nannanews.activity_content import FIELD_LIMITS, activity_citations
from nannanews.bulletin_common import source_url

VERSION = 'historical-evidence-refs-v1'
CHUNK_CHARS = 4000
OVERLAP_CHARS = 512
MAX_CATALOG_ENTRIES = 4096
MAX_CATALOG_TEXT_CHARS = 8_000_000
SCHEMA_VERSION = 'historical-evidence-json-schema-v2'


def historical_response_schema(catalog, sources):
    """Request-bound strict schema; canonical evidence validation remains mandatory.

    Use the documented OpenAI strict subset (objects/arrays/anyOf/$defs/enums).
    Relational same-URL/same-image and literal evidence checks remain in the
    unchanged decoder and V5 validators. Reject unrepresentable schemas before
    payment, never shorten enums, downgrade JSON mode, or split/retry requests.
    """
    def obj(properties):
        return {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False}

    def array(items, maximum, minimum=0):
        return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}

    def string(maximum, minimum=1):
        return {'type': 'string', 'minLength': minimum, 'maxLength': maximum}

    def enum(values, kind='string'):
        return {'type': kind, 'enum': values}

    def ref(name):
        return {'$ref': '#/$defs/' + name}

    def union(choices):
        return {'anyOf': choices} if len(choices) > 1 else choices[0]

    entries = [entry for entry in catalog['entries'] if entry['text'].strip()]
    primary_ids = [entry['id'] for entry in entries if entry['origin'] == 'primary.text']
    bulletin_ids = [entry['id'] for entry in entries if entry['origin'] != 'primary.text']
    urls = [source['url'] for source in sources]
    indices = sorted({image['image_index'] for source in sources for image in source.get('images', [])})
    definitions = {}
    claim_refs, common_quotes, field_evidence = [], [], []
    if urls:
        definitions['primary_url'] = enum(urls)
    if primary_ids:
        definitions['primary_ref'] = enum(primary_ids)
        claim_refs.append(ref('primary_ref'))
        common_quotes.append(obj({'ref': ref('primary_ref')}))
        field_evidence.append(obj({'url': ref('primary_url'), 'ref': ref('primary_ref')}))
    if bulletin_ids:
        definitions['bulletin_ref'] = enum(bulletin_ids)
        claim_refs.append(ref('bulletin_ref'))
    image_claim = None
    if indices:
        definitions['image_index'] = enum(indices, 'integer')
        common_quotes.append(obj({'text': string(12000), 'image_index': ref('image_index')}))
        image_claim = obj({'url': ref('primary_url'), 'text': string(12000),
                           'image_index': ref('image_index')})
        field_evidence.append(obj({'url': ref('primary_url'), 'text': string(4000),
                                   'image_index': ref('image_index')}))
    claim_quotes = [obj({'ref': union(claim_refs)})] if claim_refs else []
    if image_claim is not None:
        claim_quotes.append(image_claim)
    if field_evidence:
        definitions['field_evidence'] = union(field_evidence)
        activity_fields = {name: {'anyOf': [
            obj({'value': string(limit), 'evidence': array(ref('field_evidence'), 8, 1)}),
            obj({'value': {'type': 'null'}, 'evidence': array(ref('field_evidence'), 0)})]}
            for name, limit in FIELD_LIMITS.items()}
        # Select the literal evidence before generating its college name and role.
        activity_fields['colleges'] = array(obj({'evidence': ref('field_evidence'),
            'name': string(200), 'relation': enum(['associated', 'audience'])}), 30)
        activity_fields['image_indices'] = array(ref('image_index'), 64) if indices else array({'type': 'integer'}, 0)
        activity = {'anyOf': [obj(activity_fields), {'type': 'null'}]}
    else:
        activity = {'type': 'null'}
    if claim_quotes:
        definitions['claim_kind'] = enum(['event', 'deadline', 'registration', 'application', 'listed', 'source'])
        definitions['claim_quote'] = union(claim_quotes)
        claims = array(union([obj({'kind': ref('claim_kind'), 'quote': ref('claim_quote'),
            'resolution': enum(resolutions), 'normalized_date': {'type': day_type},
            'evidence': string(4000), 'locator': {'type': 'null'}})
            for day_type, resolutions in (
                ('string', ['explicit', 'contextual']),
                ('null', ['year_unknown', 'ambiguous', 'invalid']))]), 1000)
    else:
        claims = array({'type': 'null'}, 0)
    citations = array(obj({'url': ref('primary_url'), 'quotes': array(union(common_quotes), 100, 1)}), 100) if common_quotes else array({'type': 'null'}, 0)
    # Structured outputs follow property order: emit date claims before the verdict.
    schema = obj({'evidence_scope': enum([catalog['scope']]),
        'reason': string(4000), 'claims': claims,
        'source_status': enum(['source_verified', 'source_conflict', 'source_unavailable', 'source_mismatch', 'secondary_only']),
        'source_reason': string(4000), 'citations': citations, 'activity': activity,
        'status': enum(['resolved', 'needs_context', 'source_missing', 'recurring_no_end',
                        'no_relevant_date', 'invalid_source', 'uncertain', 'unresolved'])})
    if definitions:
        schema['$defs'] = definitions
    # Current documented strict protocol limits, not our catalog's larger limits.
    enum_count = 0
    string_chars = 0

    def count(node):
        nonlocal enum_count, string_chars
        if isinstance(node, dict):
            values = node.get('enum', [])
            enum_count += len(values)
            enum_chars = sum(len(value) for value in values if isinstance(value, str))
            if len(values) > 250 and enum_chars > 15000:
                raise ValueError('Historical strict schema exceeds the provider enum string bound; no request was sent.')
            string_chars += enum_chars
            for key in ('properties', '$defs'):
                string_chars += sum(map(len, node.get(key, {})))
            for value in node.values():
                count(value)
        elif isinstance(node, list):
            for value in node:
                count(value)

    count(schema)
    if enum_count > 1000 or string_chars > 120000:
        raise ValueError('Historical strict schema exceeds the provider enum/string bound; no request was sent.')
    return schema


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def build_evidence_catalog(occurrence, sources):
    """Build complete bounded coverage, or fail before any paid reservation.

    Every primary and bulletin field obeys acquisition.MAX_TEXT; at most 100
    primary sources, 4096 fields/entries and 8M catalog text characters are
    representable. The existing acquisition request-byte/media limits apply to
    the entire resulting request too. Bounds reject, never truncate or split.
    """
    from nannanews.acquisition import MAX_TEXT

    if not isinstance(sources, list) or len(sources) > 100:
        raise ValueError('Evidence catalog requires at most 100 primary sources.')
    related = occurrence.get('related_occurrences') or []
    if not isinstance(related, list) or len(related) > (MAX_CATALOG_ENTRIES - 2) // 2:
        raise ValueError('Evidence catalog cannot completely cover related bulletin fields.')
    fields = []
    source_identities = []
    urls = set()
    linked = set(occurrence.get('source_urls') or [])
    for item in related:
        if not isinstance(item, dict):
            raise ValueError('Evidence catalog related occurrence is invalid.')
        linked.update(item.get('source_urls') or [])
    for source in sources:
        url = source.get('url')
        if (not isinstance(url, str) or source_url(url) != url or url in urls or url not in linked
                or (url.startswith('https://nik-nul.github.io/news/')
                    or url.startswith('http://nik-nul.github.io/news/'))):
            raise ValueError('Evidence catalog primary source is not uniquely linked to this item.')
        urls.add(url)
        fields.append(('primary.text', url, source.get('text')))
        source_identities.append({key: source.get(key) for key in
            ('url', 'resolved_url', 'raw_response_digest')})
        source_identities[-1]['images'] = [{key: image.get(key) for key in
            ('image_index', 'url', 'sha256')} for image in source.get('images', [])]
    for name in ('title', 'body_text'):
        fields.append(('bulletin.' + name, None, occurrence.get(name, '')))
    for index, item in enumerate(related):
        for name in ('title', 'body_text'):
            fields.append((f'bulletin.related_occurrences[{index}].{name}', None, item.get(name, '')))
    if len(fields) > MAX_CATALOG_ENTRIES:
        raise ValueError('Evidence catalog has too many original fields for complete coverage.')
    identities = []
    total = 0
    for origin, url, text in fields:
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError('Evidence catalog original field exceeds the source text bound.')
        total += len(text)
        if total > MAX_CATALOG_TEXT_CHARS:
            raise ValueError('Evidence catalog cannot completely cover the supplied text.')
        identities.append([origin, url, len(text), hashlib.sha256(text.encode('utf-8')).hexdigest()])
    scope = _digest({'version': VERSION, 'fields': identities, 'sources': source_identities,
        'occurrence': {key: occurrence.get(key) for key in
            ('source_sha256', 'local_key', 'related_context_sha256', 'issue_date')}})
    entries = []
    total = 0
    for origin, url, text in fields:
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):
                newline = text.rfind('\n', end - OVERLAP_CHARS, end)
                if newline >= 0:
                    end = newline + 1
            passage = text[start:end]
            total += len(passage)
            if len(entries) >= MAX_CATALOG_ENTRIES or total > MAX_CATALOG_TEXT_CHARS:
                raise ValueError('Evidence catalog cannot completely cover the supplied text within bounds.')
            entry = {'origin': origin, 'url': url, 'start': start, 'end': end,
                'sha256': hashlib.sha256(passage.encode('utf-8')).hexdigest(), 'text': passage}
            # Keep references compact but reject accidentally mixed request catalogs.
            entry['id'] = 'e' + scope[:16] + '-' + str(len(entries) + 1)
            entries.append(entry)
            if end == len(text):
                break
            start = end - OVERLAP_CHARS
    return {'version': VERSION, 'scope': scope, 'entries': entries}


def decode_evidence_response(response, occurrence, *, sources, mismatched=False):
    """Strictly decode this request's references without mutating any inputs."""
    catalog = build_evidence_catalog(occurrence, sources)
    keys = {'status', 'reason', 'claims', 'source_status', 'source_reason', 'citations',
        'activity', 'evidence_scope'}
    if not isinstance(response, dict) or set(response) != keys:
        raise ValueError('Historical reference response has an unsupported shape.')
    if response['evidence_scope'] != catalog['scope']:
        raise ValueError('Historical evidence references belong to a different request scope.')
    entries = {entry['id']: entry for entry in catalog['entries']}
    by_url = {source['url']: source for source in sources}

    def reference(value):
        if not isinstance(value, str) or value not in entries:
            raise ValueError('Historical evidence reference is unknown or unbound.')
        entry = entries[value]
        if not entry['text'].strip():
            raise ValueError('Historical evidence reference selects blank text.')
        return entry

    def quotation(value, url, limit):
        if not isinstance(url, str) or url not in by_url or not isinstance(value, dict):
            raise ValueError('Historical evidence quotation has no captured primary URL.')
        if set(value) == {'ref'}:
            entry = reference(value['ref'])
            if entry['origin'] != 'primary.text' or entry['url'] != url:
                raise ValueError('Historical evidence reference is not bound to this primary URL.')
            return {'text': entry['text'], 'image_index': None}
        if set(value) != {'text', 'image_index'}:
            raise ValueError('Historical evidence quotation has an unsupported shape.')
        text, index = value['text'], value['image_index']
        if (not isinstance(text, str) or not text.strip() or len(text) > limit
                or type(index) is not int or index not in
                {image['image_index'] for image in by_url[url]['images']}):
            raise ValueError('Historical image quotation requires actual text and a captured image index.')
        return dict(value)

    def field_evidence(value):
        if not isinstance(value, dict) or set(value) not in (
                {'url', 'ref'}, {'url', 'text', 'image_index'}):
            raise ValueError('Historical activity reference has an unsupported shape.')
        return dict(quotation({key: item for key, item in value.items() if key != 'url'},
            value['url'], 4000), url=value['url'])

    common = response['citations']
    if not isinstance(common, list) or len(common) > 100:
        raise ValueError('Historical reference citations exceed the canonical bound.')
    citations = []
    for citation in common:
        if not isinstance(citation, dict) or set(citation) != {'url', 'quotes'}:
            raise ValueError('Historical reference citation has an unsupported shape.')
        quotes = citation['quotes']
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 100:
            raise ValueError('Historical reference citation quotations exceed the canonical bound.')
        citations.append({'url': citation['url'], 'quotes': [
            quotation(quote, citation['url'], 12000) for quote in quotes]})

    activity = response['activity']
    if activity is not None:
        if not isinstance(activity, dict) or set(activity) != set(FIELD_LIMITS) | {'colleges', 'image_indices'}:
            raise ValueError('Historical reference activity has an unsupported shape.')
        clean = {}
        for name in FIELD_LIMITS:
            field = activity[name]
            if (not isinstance(field, dict) or set(field) != {'value', 'evidence'}
                    or not isinstance(field['evidence'], list) or len(field['evidence']) > 8):
                raise ValueError('Historical reference activity field has an unsupported shape.')
            clean[name] = {'value': field['value'], 'evidence': [field_evidence(item) for item in field['evidence']]}
        colleges = activity['colleges']
        if not isinstance(colleges, list) or len(colleges) > 30:
            raise ValueError('Historical reference colleges exceed the canonical bound.')
        clean['colleges'] = []
        for college in colleges:
            if not isinstance(college, dict) or set(college) != {'name', 'relation', 'evidence'}:
                raise ValueError('Historical reference college has an unsupported shape.')
            clean['colleges'].append(dict(college, evidence=field_evidence(college['evidence'])))
        clean['image_indices'] = activity['image_indices']
        activity = clean
    field_citations = activity_citations(activity) if activity is not None else []
    cited = [(citation['url'], quote) for citation in citations for quote in citation['quotes']]
    cited.extend((item['url'], {'text': item['text'], 'image_index': item['image_index']}) for item in field_citations)
    claims = response['claims']
    if not isinstance(claims, list) or len(claims) > 1000:
        raise ValueError('Historical reference claims exceed the canonical bound.')
    canonical_claims = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
                'kind', 'quote', 'normalized_date', 'resolution', 'evidence', 'locator'}:
            raise ValueError('Historical reference claim has an unsupported shape.')
        selected = claim['quote']
        url = None
        if isinstance(selected, dict) and set(selected) == {'ref'}:
            entry = reference(selected['ref'])
            text, url = entry['text'], entry['url']
            quote = {'text': text, 'image_index': None}
        elif isinstance(selected, dict) and set(selected) == {'url', 'text', 'image_index'}:
            url = selected['url']
            quote = quotation({'text': selected['text'], 'image_index': selected['image_index']}, url, 12000)
            text = quote['text']
        else:
            raise ValueError('Historical reference claim must explicitly select text or a captured image.')
        if url is not None:
            if mismatched or response['source_status'] not in {'source_verified', 'source_conflict'}:
                raise ValueError('Historical primary date reference requires a matched primary verdict.')
            if not any(cited_url == url and item['image_index'] == quote['image_index']
                    and text in item['text'] for cited_url, item in cited):
                target = next((item for item in citations if item['url'] == url), None)
                if target is None:
                    if len(citations) >= 100:
                        raise ValueError('Historical date citations exceed the canonical source bound.')
                    target = {'url': url, 'quotes': []}
                    citations.append(target)
                if len(target['quotes']) >= 100:
                    raise ValueError('Historical date citations exceed the canonical quotation bound.')
                target['quotes'].append(quote)
                cited.append((url, quote))
        canonical_claims.append({**{key: value for key, value in claim.items() if key != 'quote'}, 'raw_text': text})
    canonical = {**{key: value for key, value in response.items() if key not in
        {'evidence_scope', 'claims', 'citations', 'activity'}},
        'claims': canonical_claims, 'citations': citations, 'activity': activity}
    # Reference compression must not bypass the response-size assumption used by
    # the unchanged pre-payment receipt storage reservation.
    from nannanews.acquisition import MAX_MODEL_BODY
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(canonical):
        size += len(part.encode('utf-8'))
        if size > MAX_MODEL_BODY:
            raise ValueError('Historical resolved reference response exceeds the canonical response byte bound.')
    return canonical
