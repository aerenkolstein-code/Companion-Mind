"""Local-only, strict plaintext Docs edit planning. No network or secret access.

Returned objects contain private text and must never be logged or sent to a
model. A future broker must persist a protected recovery package before dispatch.
"""
from dataclasses import dataclass, field
import hashlib
import json
from .policy import Denied, require

MAX_TEXT = 32768
MAX_RESPONSE = 1048576


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def utf16(text):
    try:
        return len(text.encode('utf-16-le')) // 2
    except UnicodeError:
        raise Denied('TEXT_INVALID') from None


def checked_text(text):
    require(type(text) is str, 'TEXT_INVALID')
    try:
        require(len(text.encode('utf-8')) <= MAX_TEXT, 'TEXT_LIMIT')
    except UnicodeError:
        raise Denied('TEXT_INVALID') from None
    require(all(c in '\n\t' or (ord(c) >= 32 and not 0x7f <= ord(c) <= 0x9f
                               and not 0xe000 <= ord(c) <= 0xf8ff) for c in text), 'TEXT_INVALID')
    return text


def obj(value, allowed, required=()):
    require(type(value) is dict and set(value) <= set(allowed) and set(required) <= set(value),
            'UNSUPPORTED_DOCUMENT')
    return value


def no_suggestions(value):
    if type(value) is dict:
        for key, child in value.items():
            require(type(key) is str and not ('suggest' in key.lower() and key != 'suggestionsViewMode' and child),
                    'UNSUPPORTED_SUGGESTIONS')
            no_suggestions(child)
    elif type(value) is list:
        for child in value:
            no_suggestions(child)


@dataclass(frozen=True, repr=False)
class Snapshot:
    document_id: str
    tab_id: str
    revision: str
    text: str = field(repr=False)
    raw: bytes = field(repr=False)
    style_hash: str

    @property
    def text_hash(self):
        return digest(self.text.encode('utf-8'))


def parse_document(document, expected_id):
    """Require includeTabsContent=true and SUGGESTIONS_INLINE, never a projection."""
    try:
        raw = canonical(document)
        require(len(raw) <= MAX_RESPONSE, 'RESPONSE_LIMIT')
        obj(document, {'documentId', 'title', 'revisionId', 'suggestionsViewMode', 'tabs'},
            {'documentId', 'revisionId', 'suggestionsViewMode', 'tabs'})
        require(document['documentId'] == expected_id and type(expected_id) is str and bool(expected_id), 'DOCUMENT_MISMATCH')
        revision = document['revisionId']
        require(type(revision) is str and 0 < len(revision) <= 1024, 'REVISION_REQUIRED')
        require(document['suggestionsViewMode'] == 'SUGGESTIONS_INLINE', 'SUGGESTIONS_VIEW_REQUIRED')
        no_suggestions(document)
        tabs = document['tabs']
        require(type(tabs) is list and len(tabs) == 1, 'UNSUPPORTED_TABS')
        tab = obj(tabs[0], {'tabProperties', 'childTabs', 'documentTab'}, {'tabProperties', 'documentTab'})
        require(not tab.get('childTabs'), 'UNSUPPORTED_TABS')
        props = obj(tab['tabProperties'], {'tabId', 'title', 'index', 'parentTabId'}, {'tabId'})
        require(not props.get('parentTabId') and props.get('index', 0) == 0, 'UNSUPPORTED_TABS')
        tab_id = props['tabId']
        require(type(tab_id) is str and 0 < len(tab_id) <= 128, 'UNSUPPORTED_TABS')
        content = obj(tab['documentTab'], {'body', 'documentStyle', 'namedStyles'}, {'body'})
        body = obj(content['body'], {'content'}, {'content'})['content']
        require(type(body) is list and len(body) >= 2, 'UNSUPPORTED_DOCUMENT')
        section = obj(body[0], {'startIndex', 'endIndex', 'sectionBreak'}, {'endIndex', 'sectionBreak'})
        require(section.get('startIndex', 0) == 0 and section['endIndex'] == 1, 'UNSUPPORTED_DOCUMENT')
        obj(section['sectionBreak'], {'sectionStyle'})
        # No linked headers/footers, columns, or other non-plaintext section state.
        section_style = obj(section['sectionBreak'].get('sectionStyle', {}), {'columnSeparatorStyle', 'contentDirection', 'sectionType'})
        require(section_style.get('columnSeparatorStyle', 'NONE') == 'NONE'
                and section_style.get('sectionType', 'CONTINUOUS') == 'CONTINUOUS', 'UNSUPPORTED_DOCUMENT')
        cursor, parts, common_style = 1, [], None
        for block in body[1:]:
            obj(block, {'startIndex', 'endIndex', 'paragraph'}, {'startIndex', 'endIndex', 'paragraph'})
            require(type(block['startIndex']) is int and block['startIndex'] == cursor, 'INDEX_MISMATCH')
            para = obj(block['paragraph'], {'elements', 'paragraphStyle'}, {'elements'})
            style = obj(para.get('paragraphStyle', {}), {'namedStyleType', 'direction'})
            require(style.get('namedStyleType', 'NORMAL_TEXT') == 'NORMAL_TEXT'
                    and style.get('direction', 'LEFT_TO_RIGHT') == 'LEFT_TO_RIGHT', 'UNSUPPORTED_STYLE')
            if common_style is None:
                common_style = style
            require(style == common_style, 'UNSUPPORTED_STYLE')
            elements = para['elements']
            require(type(elements) is list and bool(elements), 'UNSUPPORTED_DOCUMENT')
            para_text = ''
            for element in elements:
                obj(element, {'startIndex', 'endIndex', 'textRun'}, {'startIndex', 'endIndex', 'textRun'})
                run = obj(element['textRun'], {'content', 'textStyle'}, {'content'})
                require(run.get('textStyle', {}) == {}, 'UNSUPPORTED_STYLE')
                text = checked_text(run['content'])
                require(bool(text) and type(element['startIndex']) is int and element['startIndex'] == cursor
                        and type(element['endIndex']) is int and element['endIndex'] == cursor + utf16(text), 'INDEX_MISMATCH')
                para_text += text
                cursor = element['endIndex']
            require(para_text.endswith('\n') and '\n' not in para_text[:-1], 'UNSUPPORTED_DOCUMENT')
            require(type(block['endIndex']) is int and block['endIndex'] == cursor, 'INDEX_MISMATCH')
            parts.append(para_text)
        text = checked_text(''.join(parts))
        require(text.endswith('\n'), 'FINAL_NEWLINE_REQUIRED')
        style_hash = digest(canonical({'paragraph': common_style, 'section': section_style,
                                     'document': content.get('documentStyle', {}), 'named': content.get('namedStyles', {})}))
        return Snapshot(expected_id, tab_id, revision, text, raw, style_hash)
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
        raise Denied('UNSUPPORTED_DOCUMENT') from None


@dataclass(frozen=True, repr=False)
class EditPlan:
    before: Snapshot = field(repr=False)
    after_text: str = field(repr=False)
    request_bytes: bytes = field(repr=False)

    @property
    def request_hash(self):
        return digest(self.request_bytes)

    def verify(self, after):
        require(after.document_id == self.before.document_id and after.tab_id == self.before.tab_id
                and after.style_hash == self.before.style_hash and after.text == self.after_text,
                'READBACK_MISMATCH')


def _edit(snapshot, start, end, replacement):
    # Offsets here are Python character positions; only requests use UTF-16.
    require(type(snapshot) is Snapshot, 'SNAPSHOT_REQUIRED')
    require(type(start) is int and type(end) is int and 0 <= start <= end < len(snapshot.text), 'EDIT_RANGE_INVALID')
    checked_text(replacement)
    require(start != end or bool(replacement), 'NO_CHANGE')
    after = checked_text(snapshot.text[:start] + replacement + snapshot.text[end:])
    require(after.endswith('\n') and after != snapshot.text, 'NO_CHANGE')
    lo, hi = 1 + utf16(snapshot.text[:start]), 1 + utf16(snapshot.text[:end])
    requests = []
    if lo != hi:
        requests.append({'deleteContentRange': {'range': {'tabId': snapshot.tab_id, 'startIndex': lo, 'endIndex': hi}}})
    if replacement:
        requests.append({'insertText': {'location': {'tabId': snapshot.tab_id, 'index': lo}, 'text': replacement}})
    request = {'writeControl': {'requiredRevisionId': snapshot.revision}, 'requests': requests}
    return EditPlan(snapshot, after, canonical(request))


def replace_unique(snapshot, old, replacement):
    checked_text(old)
    require(bool(old) and '\n' not in old and snapshot.text.count(old) == 1, 'MATCH_NOT_UNIQUE')
    start = snapshot.text.index(old)
    return _edit(snapshot, start, start + len(old), replacement)


def delete_paragraph(snapshot, paragraph_number):
    require(type(paragraph_number) is int and paragraph_number >= 0, 'PARAGRAPH_INVALID')
    paragraphs = snapshot.text.splitlines(keepends=True)
    require(paragraph_number < len(paragraphs), 'PARAGRAPH_INVALID')
    start = sum(map(len, paragraphs[:paragraph_number]))
    end = min(start + len(paragraphs[paragraph_number]), len(snapshot.text) - 1)
    return _edit(snapshot, start, end, '')


def insert_text(snapshot, offset, text):
    return _edit(snapshot, offset, offset, text)


def rollback(plan, current):
    """Do not overwrite an external edit; use the current readback's revision."""
    plan.verify(current)
    return _edit(current, 0, len(current.text) - 1, plan.before.text[:-1])


def empty_created_document(snapshot):
    return _edit(snapshot, 0, len(snapshot.text) - 1, '')
