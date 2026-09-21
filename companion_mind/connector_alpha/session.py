"""One manual read, structured text projection and local-first revocation."""
from __future__ import annotations
from datetime import datetime
import json
from threading import RLock
from .contract import Denied, MIME, fingerprint, receipt, require

def _drive(value, binding):
    required = {'id', 'mimeType', 'version', 'modifiedTime', 'trashed', 'isAppAuthorized'}
    require(type(value) is dict and set(value) == required, 'PROVIDER_RESPONSE_INVALID')
    require(value['id'] == binding.file_id and value['mimeType'] == MIME
            and value['trashed'] is False and value['isAppAuthorized'] is True, 'RESOURCE_BINDING_MISMATCH')
    require(type(value['version']) is str and value['version'].isdigit()
            and len(value['version']) <= 40, 'VERSION_UNAVAILABLE')
    require(type(value['modifiedTime']) is str, 'VERSION_UNAVAILABLE')
    try:
        stamp = datetime.fromisoformat(value['modifiedTime'].replace('Z', '+00:00'))
        require(stamp.tzinfo is not None, 'VERSION_UNAVAILABLE')
    except (TypeError, ValueError):
        raise Denied('VERSION_UNAVAILABLE') from None
    return {'version': value['version'], 'modified_at': value['modifiedTime']}

def _docs(value, binding):
    require(type(value) is dict and value.get('documentId') == binding.file_id, 'RESOURCE_BINDING_MISMATCH')
    # Google only exposes revisionId to users with edit access. Absence is
    # allowed only with three equal complete responses and stable Drive version.
    if 'revisionId' not in value:
        return None
    revision = value['revisionId']
    require(type(revision) is str and 0 < len(revision) <= 2048, 'REVISION_UNAVAILABLE')
    return revision

def _coverage(body, binding):
    require(type(body) is dict and body.get('documentId') == binding.file_id, 'RESOURCE_BINDING_MISMATCH')
    tabs = body.get('tabs')
    if type(tabs) is not list or not tabs:
        return 'UNKNOWN', None
    seen = set()

    def text_content(content, depth=0):
        if type(content) is not list or depth > 32:
            return None
        output = []
        for block in content:
            if type(block) is not dict:
                return None
            kinds = set(block) - {'startIndex', 'endIndex'}
            if kinds == {'paragraph'}:
                paragraph = block['paragraph']
                if type(paragraph) is not dict or set(paragraph) - {'elements', 'paragraphStyle', 'bullet', 'positionedObjectIds'}:
                    return None
                if paragraph.get('positionedObjectIds'):
                    return None
                elements = paragraph.get('elements')
                if type(elements) is not list:
                    return None
                for element in elements:
                    if type(element) is not dict:
                        return None
                    ekinds = set(element) - {'startIndex', 'endIndex'}
                    if ekinds == {'textRun'}:
                        run = element['textRun']
                        if type(run) is not dict or set(run) - {'content', 'textStyle'} or type(run.get('content')) is not str:
                            return None
                        output.append(run['content'])
                    elif ekinds in ({'pageBreak'}, {'columnBreak'}, {'horizontalRule'}):
                        output.append('\n')
                    elif ekinds == {'footnoteReference'}:
                        ref = element['footnoteReference']
                        if type(ref) is not dict or type(ref.get('footnoteId')) is not str:
                            return None
                        output.append('[footnote:' + ref['footnoteId'] + ']')
                    else:
                        return None
            elif kinds == {'table'}:
                table = block['table']
                if type(table) is not dict or set(table) - {'rows', 'columns', 'tableRows', 'tableStyle'}:
                    return None
                rows = table.get('tableRows')
                if type(rows) is not list:
                    return None
                for row in rows:
                    if type(row) is not dict or set(row) - {'startIndex', 'endIndex', 'tableCells', 'tableRowStyle'}:
                        return None
                    cells = row.get('tableCells')
                    if type(cells) is not list:
                        return None
                    values = []
                    for cell in cells:
                        if type(cell) is not dict or set(cell) - {'startIndex', 'endIndex', 'content', 'tableCellStyle'}:
                            return None
                        nested = text_content(cell.get('content'), depth + 1)
                        if nested is None:
                            return None
                        values.append(nested)
                    output.append('\t'.join(values) + '\n')
            elif kinds == {'sectionBreak'}:
                if type(block['sectionBreak']) is not dict:
                    return None
            elif kinds == {'tableOfContents'}:
                toc = block['tableOfContents']
                nested = text_content(toc.get('content') if type(toc) is dict else None, depth + 1)
                if nested is None:
                    return None
                output.append(nested)
            else:
                return None
        return ''.join(output)

    def tab_projection(tab, depth=0):
        if depth > 32 or type(tab) is not dict or set(tab) - {'tabProperties', 'documentTab', 'childTabs'}:
            return None
        props = tab.get('tabProperties')
        if type(props) is not dict or type(props.get('tabId')) is not str or not props['tabId'] or props['tabId'] in seen:
            return None
        seen.add(props['tabId'])
        doc = tab.get('documentTab')
        known = {'body', 'headers', 'footers', 'footnotes', 'documentStyle', 'namedStyles', 'lists',
                 'namedRanges', 'inlineObjects', 'positionedObjects', 'suggestedDocumentStyleChanges',
                 'suggestedNamedStylesChanges'}
        if type(doc) is not dict or 'body' not in doc or set(doc) - known:
            return None
        if any(doc.get(key) for key in ('inlineObjects', 'positionedObjects', 'suggestedDocumentStyleChanges', 'suggestedNamedStylesChanges')):
            return None
        b = doc['body']
        if type(b) is not dict or set(b) - {'content'}:
            return None
        text = text_content(b.get('content'))
        if text is None:
            return None
        segments = {}
        for section, id_name in [('headers', 'headerId'), ('footers', 'footerId'), ('footnotes', 'footnoteId')]:
            values = doc.get(section, {})
            if type(values) is not dict:
                return None
            segments[section] = {}
            for key, value in values.items():
                if type(value) is not dict or set(value) - {id_name, 'content'}:
                    return None
                content = text_content(value.get('content'))
                if content is None:
                    return None
                segments[section][key] = content
        children = tab.get('childTabs', [])
        if type(children) is not list:
            return None
        children = [tab_projection(child, depth + 1) for child in children]
        if any(child is None for child in children):
            return None
        return {'tab_id': props['tabId'], 'text': text, **segments, 'child_tabs': children}

    projection = [tab_projection(tab) for tab in tabs]
    if any(tab is None for tab in projection):
        return 'PARTIAL', None
    return 'FULL', {'document_id': binding.file_id, 'source_url': 'https://docs.google.com/document/d/' + binding.file_id + '/edit',
                    'revision_id': body.get('revisionId'), 'coverage_scope': 'SUPPORTED_DOCUMENT_TEXT',
                    'tabs': projection}

class ConnectorSession:
    def __init__(self, binding, broker, transport):
        self.binding, self.broker, self.transport = binding, broker, transport
        self.closed, self.started, self.delivered = False, False, False
        self._lock, self._result = RLock(), None
        self._result_digest = None

    def _check(self):
        require(not self.closed, 'LOCAL_AUTHORIZATION_CLOSED')
        self.broker.assert_active()

    def _read(self, operation, token):
        with self._lock, self.broker.synchronized():
            self._check()
            value = self.transport.read(operation, token)
            require(token not in json.dumps(value, ensure_ascii=False), 'SECRET_OUTPUT_BLOCKED')
            self._check()
            return value

    def read_once(self):
        with self._lock:
            require(not self.closed and not self.started, 'READ_ALREADY_CONSUMED')
            self.started = True
            self.broker.begin_read()
            token = self.broker.acquire()
        try:
            identity = self._read('identity', token)
            user = identity.get('user') if type(identity) is dict else None
            require(type(user) is dict and user.get('emailAddress') == self.binding.subject_email
                    and user.get('permissionId') == self.binding.subject_permission_id, 'IDENTITY_MISMATCH')
            m0 = _drive(self._read('drive_before', token), self.binding)
            before = self._read('docs_before', token)
            d0 = _docs(before, self.binding)
            body = self._read('body', token)
            db = _docs(body, self.binding)
            after = self._read('docs_after', token)
            d1 = _docs(after, self.binding)
            m1 = _drive(self._read('drive_after', token), self.binding)
            require(m0 == m1 and d0 == db == d1, 'VERSION_CHANGED')
            if d0 is None:
                require(fingerprint(before) == fingerprint(body) == fingerprint(after), 'VERSION_CHANGED')
                consistency_basis = 'DRIVE_VERSION_AND_DOCS_RESPONSE_DIGEST'
            else:
                consistency_basis = 'DRIVE_VERSION_AND_DOCS_REVISION'
            coverage, content = _coverage(body, self.binding)
            with self._lock, self.broker.synchronized():
                self._check()
                status = 'SUCCESS' if coverage == 'FULL' else 'UNKNOWN'
                evidence = receipt(status, 'EXACT_READ_AS_OF' if status == 'SUCCESS' else 'COVERAGE_INCOMPLETE',
                    google_reads=6, credential_dereferences=1, content_delivered=False,
                    coverage=coverage, coverage_scope='SUPPORTED_DOCUMENT_TEXT', freshness='AS_OF',
                    provider_atomicity='UNKNOWN', drive_version=m0['version'], docs_revision=d0,
                    docs_revision_status='NOT_RETURNED' if d0 is None else 'AVAILABLE',
                    consistency_basis=consistency_basis)
                if content is not None:
                    content['read_at'] = evidence['observed_at']
                    evidence['content_digest'] = fingerprint(content)
                self._result = {'content': content, 'receipt': evidence}
                self._result_digest = fingerprint(self._result)
                return self._result
        finally:
            token = None

    def deliver(self, result, writer):
        with self._lock, self.broker.synchronized():
            self._check()
            require(result is self._result and fingerprint(result) == self._result_digest
                    and not self.delivered and result['receipt']['status'] == 'SUCCESS', 'DELIVERY_DENIED')
            writer(result['content'])
            self.delivered = True
            result['receipt']['content_delivered'] = True

    def revoke(self):
        with self._lock:
            self.closed = True
            self._result = None
            local, deleted, remote, token = False, False, 'UNKNOWN', None
            try:
                self.broker.close_local()
                local = True
            except Exception:
                pass
            try:
                token = self.broker.token_for_cleanup()
            except Exception:
                pass
            if token is not None:
                try:
                    self.transport.revoke(token)
                    remote = 'REVOKED'
                except Exception:
                    remote = 'UNKNOWN'
            try:
                self.broker.close_and_delete()
                deleted, local = True, True
            except Exception:
                pass
            finally:
                token = None
            return receipt('REVOKED' if local and deleted and remote == 'REVOKED' else 'CLEANUP_INCOMPLETE',
                'LOCAL_CLOSED_REMOTE_REVOKED' if local and deleted and remote == 'REVOKED' else 'CLEANUP_PARTIAL',
                local_closed=local, remote_state=remote, credential_deleted=deleted,
                remote_revocations=getattr(self.transport, 'counts', {}).get('remote_revocations', 'UNKNOWN'))
