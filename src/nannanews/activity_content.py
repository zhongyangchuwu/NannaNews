"""Normalized historical content, grounded only in admitted primary citations."""

FIELD_LIMITS = {
    'title': 250, 'summary': 1500, 'content': 8000, 'venue': 500,
    'organizer': 500, 'audience': 1500, 'participation': 3000, 'time': 1000,
}
REQUIRED_FIELDS = frozenset({'title', 'summary', 'content'})


def _literal_citation(value):
    if not isinstance(value, dict) or set(value) != {'url', 'text', 'image_index'}:
        raise ValueError('Activity citation has an unsupported shape.')
    url, text, index = value['url'], value['text'], value['image_index']
    if (not isinstance(url, str) or not url
            or not isinstance(text, str) or not text.strip() or len(text) > 4000
            or (index is not None and (type(index) is not int or index < 0))):
        raise ValueError('Activity citation identity or literal text is invalid.')
    return {'url': url, 'text': text, 'image_index': index}


def _activity_fields(activity, citation):
    if not isinstance(activity, dict) or set(activity) != set(FIELD_LIMITS) | {'colleges', 'image_indices'}:
        raise ValueError('Verified activity has an unsupported shape.')
    clean = {}
    for name, limit in FIELD_LIMITS.items():
        field = activity[name]
        if not isinstance(field, dict) or set(field) != {'value', 'evidence'}:
            raise ValueError('Activity field has an unsupported shape: ' + name)
        value, evidence = field['value'], field['evidence']
        if not isinstance(evidence, list) or len(evidence) > 8:
            raise ValueError('Activity field evidence is invalid: ' + name)
        if value is None:
            if name in REQUIRED_FIELDS or evidence:
                raise ValueError('Required or unknown activity field has invalid null evidence: ' + name)
        elif not isinstance(value, str) or not value.strip() or len(value) > limit or not evidence:
            raise ValueError('Known activity field requires bounded content and evidence: ' + name)
        clean[name] = {'value': value, 'evidence': [citation(item) for item in evidence]}

    colleges = activity['colleges']
    if not isinstance(colleges, list) or len(colleges) > 30:
        raise ValueError('Activity colleges must be a bounded list.')
    clean['colleges'] = []
    seen = set()
    for college in colleges:
        if not isinstance(college, dict) or set(college) != {'name', 'relation', 'evidence'}:
            raise ValueError('Activity college has an unsupported shape.')
        name, relation = college['name'], college['relation']
        if (not isinstance(name, str) or not name.strip() or len(name) > 200
                or not isinstance(relation, str) or relation not in {'associated', 'audience'}):
            raise ValueError('Activity college name or relation is invalid.')
        evidence = citation(college['evidence'])
        if name not in evidence['text']:
            raise ValueError('Activity college name must occur in the same relation quotation.')
        key = (name, relation, evidence['url'], evidence['text'], evidence['image_index'])
        if key not in seen:
            seen.add(key)
            clean['colleges'].append({'name': name, 'relation': relation, 'evidence': evidence})

    indices = activity['image_indices']
    if (not isinstance(indices, list) or len(indices) > 64
            or any(type(index) is not int or index < 0 for index in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError('Activity images must be distinct bounded captured image indices.')
    clean['image_indices'] = list(indices)
    return clean


def activity_citations(activity):
    """Collect actual field/college citations after validating the full activity shape.

    This performs no source admission. Callers must admit the canonical union with
    common citations, then validate_activity against that admitted source evidence.
    """
    citations = []

    def collect(value):
        checked = _literal_citation(value)
        citations.append(checked)
        return checked

    _activity_fields(activity, collect)
    return citations


def validate_activity(activity, sources):
    """Validate content against already admitted same-source, same-image evidence."""
    by_url = {source['url']: source for source in sources}

    def citation(value):
        checked = _literal_citation(value)
        url, text, index = checked['url'], checked['text'], checked['image_index']
        if url not in by_url or not any(index == quote['image_index'] and text in quote['text']
                                       for quote in by_url[url]['quote_evidence']):
            raise ValueError('Activity citation is absent from the same source and image admitted quotation.')
        return checked

    clean = _activity_fields(activity, citation)
    allowed_images = {image['image_index'] for source in sources if source['quote_evidence']
                      for image in source['images']}
    if any(index not in allowed_images for index in clean['image_indices']):
        raise ValueError('Activity images must come from cited primary sources.')
    return clean


def public_activity(activity):
    """Whitelist an already validated activity; never include private source data."""
    if activity is None:
        return None

    def citation(value):
        return {key: value[key] for key in ('url', 'text', 'image_index')}

    return {
        **{name: {'value': activity[name]['value'],
                  'evidence': [citation(item) for item in activity[name]['evidence']]}
           for name in FIELD_LIMITS},
        'colleges': [{'name': item['name'], 'relation': item['relation'],
                      'evidence': citation(item['evidence'])} for item in activity['colleges']],
        'image_indices': list(activity['image_indices']),
    }
